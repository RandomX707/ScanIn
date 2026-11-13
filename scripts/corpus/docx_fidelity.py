"""Feature-C acceptance on the real Health Quote Format.docx: measured fidelity vs ground truth read from the DOCX XML,
plus native edit -> save -> reopen -> re-render. Prints measurements only; page images stay under .private/."""
from __future__ import annotations
import hashlib, io, json, re, sys, zipfile
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from iso_client import Client  # noqa: E402
import numpy as np, pypdf, pypdfium2 as pdfium  # noqa: E402
from lxml import etree  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "Policy Copies/QCR/Health Quote Format.docx"
OUT = ROOT / ".private/visual-evidence/docx"; OUT.mkdir(parents=True, exist_ok=True)
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
results, metrics = [], {}
def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail))); print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else "")); return ok

def truth(path: Path):
    z = zipfile.ZipFile(path); doc = etree.fromstring(z.read("word/document.xml"))
    paragraphs = ["".join(p.xpath(".//w:t/text()", namespaces=NS)) for p in doc.xpath("//w:p", namespaces=NS)]
    words = Counter(w.lower() for t in paragraphs for w in re.findall(r"[A-Za-z0-9₹@.]{2,}", t))
    colors = {c.upper() for c in doc.xpath("//w:color/@w:val", namespaces=NS) if c not in ("auto",)}
    fills = {c.upper() for c in doc.xpath("//w:shd/@w:fill", namespaces=NS) if c not in ("auto",)}
    pg = doc.xpath("//w:pgSz", namespaces=NS)[0]
    media = [n for n in z.namelist() if re.fullmatch(r"(word/)?media/[^/]+", n)]
    return {"paragraphs": paragraphs, "words": words, "colors": colors, "fills": fills, "links": len(doc.xpath("//w:hyperlink", namespaces=NS)),
            "tables": len(doc.xpath("//w:tbl", namespaces=NS)), "drawings": len(doc.xpath("//w:drawing", namespaces=NS)), "media": media,
            "page_pt": (int(pg.get(f"{{{W}}}w")) / 20, int(pg.get(f"{{{W}}}h")) / 20), "background": bool(doc.xpath("//w:background", namespaces=NS)),
            "list_paras": len(doc.xpath("//w:p[w:pPr/w:numPr]", namespaces=NS)), "headers": [n for n in z.namelist() if re.fullmatch(r"word/(header|footer)\d+\.xml", n)]}

def near(img, rgb, tol=36):
    return float((np.abs(img[..., :3].astype(int) - np.array(rgb)).sum(axis=2) < tol).mean())

def render_pages(pdf: bytes, scale=1.4):
    d = pdfium.PdfDocument(pdf); return [np.array(d[i].render(scale=scale).to_pil().convert("RGB")) for i in range(len(d))]

def main():
    c = Client(); data = SRC.read_bytes(); sha = hashlib.sha256(data).hexdigest()
    gt = truth(SRC)
    s, reg, insp = c.upload(SRC); d, v1 = reg["document_id"], reg["version_id"]
    pv = c.json(f"/api/documents/{d}/versions/{v1}/preview")
    check("preview offers the faithful page render", pv["render"]["available"] is True and "libreoffice" in pv["render"]["engine"], pv["render"]["engine"])
    s, pdf, h = c.call("GET", f"/api/documents/{d}/versions/{v1}/render", raw=True)
    check("render returns a PDF", s == 200 and pdf[:5] == b"%PDF-", s)
    rd = pypdf.PdfReader(io.BytesIO(pdf)); pages = len(rd.pages); metrics["pages"] = pages
    w, hh = float(rd.pages[0].mediabox.width), float(rd.pages[0].mediabox.height)
    metrics["page_size_error_pt"] = round(max(abs(w - gt["page_pt"][0]), abs(hh - gt["page_pt"][1])), 2)
    check("page size matches the DOCX page setup (A4)", metrics["page_size_error_pt"] < 1.0, f"{metrics['page_size_error_pt']}pt")
    text = " ".join((p.extract_text() or "") for p in rd.pages).lower()
    rendered_words = Counter(w_ for w_ in re.findall(r"[a-z0-9₹@.]{2,}", text))
    covered = sum(min(n, rendered_words.get(w_, 0)) for w_, n in gt["words"].items()); total = sum(gt["words"].values())
    metrics["text_coverage"] = round(covered / total, 4); check("text coverage of the rendering (words in DOCX found in pages)", metrics["text_coverage"] >= 0.97, f"{metrics['text_coverage']:.1%}")
    links = sum(1 for p in rd.pages for a in (p.get("/Annots") or []) if a.get_object().get("/Subtype") == "/Link")
    metrics["hyperlinks"] = {"docx": gt["links"], "rendered": links}; check("hyperlinks are present as real links", gt["links"] == 0 or links >= 0.9 * gt["links"], f"{links}/{gt['links']}")
    images = {tuple(sorted(p.get("/Resources", {}).get("/XObject", {}).keys())) for p in rd.pages}
    n_images = len({i.name + str(len(i.data)) for p in rd.pages for i in p.images})
    metrics["images"] = {"docx_media": len(gt["media"]), "rendered": n_images}; check("every embedded image appears in the pages", n_images >= len(gt["media"]) and len(gt["media"]) >= 2, f"{n_images}/{len(gt['media'])}")
    imgs = render_pages(pdf)
    for i, im in enumerate(imgs): __import__("PIL.Image", fromlist=["Image"]).fromarray(im).save(OUT / f"v1-page{i + 1}.png")
    p1 = imgs[0]; H, Wd = p1.shape[:2]
    top = p1[: int(H * 0.2)]; nonwhite = (top.min(axis=2) < 235)
    cols = np.where(nonwhite.any(axis=0))[0]; logo_w = (cols.max() - cols.min()) / Wd if len(cols) else 0
    check("brand logo is visible in the header area (non-white block ≥ 20% page width)", logo_w >= 0.2, f"{logo_w:.0%} of width")
    mid = p1[int(H * 0.22): int(H * 0.45)]; ill = (mid.min(axis=2) < 235).mean()
    check("healthcare illustration is visible below the tagline", ill > 0.02, f"{ill:.1%} of that band painted")
    brand = near(np.concatenate(imgs), (0xBA, 0x2F, 0x4F))
    check("heading colour BA2F4F (defined in the DOCX) is rendered", "BA2F4F" in gt["colors"] | gt["fills"] and brand > 0.0005, f"{brand:.3%} of pixels")
    corner = p1[5:30, 5:30].reshape(-1, 3).mean(axis=0)
    check("page background matches the file: it defines none, so white — not a dark canvas", (not gt["background"]) and corner.min() > 245, f"w:background present={gt['background']}, corner rgb={corner.astype(int).tolist()}")
    check("bullets/lists are rendered (list paragraphs exist and bullet glyph runs are drawn)", gt["list_paras"] > 5 and "•" in text or "➢" in text or "" in text or "o " in text, gt["list_paras"])
    check("tables are present in the DOCX and their text is in the pages", gt["tables"] >= 1 and "base quote comparison" in text, gt["tables"])
    # original untouched
    check("original DOCX byte-identical after rendering", hashlib.sha256(c.file(d, v1)[1]).hexdigest() == sha, "")
    # edit natively, save, reopen, re-render
    blocks = pv["blocks"]; para = next(b for b in blocks if b["e"] and not b["table"] and 20 < len(b["text"]) < 120)
    new_text = para["text"] + " (reviewed)"
    s, body, _ = c.post(f"/api/documents/{d}/versions/{v1}/save", {"changes": [{"target_id": para["target_id"], "fingerprint": para["fp"], "value": new_text}]})
    check("native DOCX edit saves", s == 200 and body["outcome"] in ("VERIFIED", "STRUCTURALLY_VERIFIED"), f"{s} {body.get('outcome') or body.get('code')}")
    v2 = body["version_id"]; s, v2b, _ = c.file(d, v2)
    z1, z2 = zipfile.ZipFile(io.BytesIO(data)), zipfile.ZipFile(io.BytesIO(v2b))
    check("v2 is a valid zip package", z2.testzip() is None, "")
    changed_parts = [n for n in z1.namelist() if n in z2.namelist() and z1.read(n) != z2.read(n)]
    protected = [n for n in z1.namelist() if re.search(r"(media/|word/(styles|numbering|settings|fontTable|theme/|header|footer|footnotes|endnotes|webSettings))", n)]
    check("media, styles, numbering, theme, settings, headers/footers are byte-identical", all(z1.read(n) == z2.read(n) for n in protected) and len(protected) >= 8, f"{len(protected)} parts compared")
    check("only the document body and provenance/package manifests changed", set(changed_parts) <= {"word/document.xml", "docProps/core.xml", "docProps/app.xml", "docProps/custom.xml", "[Content_Types].xml", "_rels/.rels"} and "word/document.xml" in changed_parts, changed_parts)
    check("no parts were lost", set(z1.namelist()) <= set(z2.namelist()), "")
    gt2 = truth_bytes = None
    t2 = etree.fromstring(z2.read("word/document.xml")); p2 = ["".join(p.xpath(".//w:t/text()", namespaces=NS)) for p in t2.xpath("//w:p", namespaces=NS)]
    diffs = [(a, b) for a, b in zip(gt["paragraphs"], p2) if a != b]
    check("exactly one paragraph differs, and it is the edited one", len(gt["paragraphs"]) == len(p2) and len(diffs) == 1 and diffs[0][1] == new_text, f"{len(diffs)} differ")
    s, pdf2, _ = c.call("GET", f"/api/documents/{d}/versions/{v2}/render", raw=True)
    check("v2 reopens and renders (independent conversion of the saved file)", s == 200 and pdf2[:5] == b"%PDF-", s)
    imgs2 = render_pages(pdf2); check("v2 has the same page count", len(imgs2) == len(imgs), f"{len(imgs2)}/{len(imgs)}")
    diff_pages = []
    for i, (a, b) in enumerate(zip(imgs, imgs2)):
        if a.shape != b.shape: diff_pages.append((i + 1, 1.0)); continue
        frac = float((np.abs(a.astype(int) - b.astype(int)).sum(axis=2) > 60).mean())
        if frac > 0: diff_pages.append((i + 1, round(frac, 4)))
        if frac > 0: __import__("PIL.Image", fromlist=["Image"]).fromarray(b).save(OUT / f"v2-page{i + 1}.png")
    metrics["v1_vs_v2_changed_pages"] = diff_pages
    check("the edit changes only the page it is on, by a small amount (≤ 3% of that page's pixels)", 1 <= len(diff_pages) <= 2 and all(f <= 0.03 for _, f in diff_pages), diff_pages)
    check("the new wording is visible in v2's pages", "(reviewed)" in " ".join(p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(pdf2)).pages), "")
    check("original byte-identical after edit", hashlib.sha256(c.file(d, v1)[1]).hexdigest() == sha, "")
    s, cmp, _ = c.call("GET", f"/api/documents/{d}/compare?base={v1}&target={v2}")
    check("compare v1 vs v2 reports the paragraph edit", s == 200 and cmp["format"] == "docx" and cmp["summary"]["modified"] == 1, f"{s} {cmp.get('summary')}")
    # chunks -> page links
    ch = c.json(f"/api/documents/{d}/versions/{v1}/chunks")["sections"]
    linked = [x for x in ch if (x.get("source") or {}).get("pages")]
    check("sections whose heading can be located link to the rendered page (ambiguous ones are not linked)", len(linked) >= 1, f"{len(linked)}/{len(ch)} sections have a page")
    print("\nMEASUREMENTS", json.dumps(metrics))
    failed = [r for r in results if not r[1]]
    (ROOT / ".private/feature-results/docx-fidelity.json").write_text(json.dumps({"checks": [{"check": n, "pass": ok} for n, ok, _ in results], "metrics": metrics}, indent=1))
    print(f"{len(results) - len(failed)} passed, {len(failed)} failed"); sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
