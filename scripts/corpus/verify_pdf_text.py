#!/usr/bin/env python3
"""Independent check of a saved PDF against its source: prints OK or the first failed check.
usage: verify_pdf_text.py SOURCE.pdf SAVED.pdf OLD_TEXT NEW_TEXT
Uses pdfium + pypdf (not the editing engine) so the verdict does not depend on the code under test."""
import re, sys
import numpy as np, pypdf, pypdfium2 as pdfium

def norm(s): return re.sub(r"\s+", "", s)
src, out, old, new = sys.argv[1:5]
a, b = open(src, "rb").read(), open(out, "rb").read()
if not b.startswith(b"%PDF-"): sys.exit("not a PDF")
da, db = pdfium.PdfDocument(a), pdfium.PdfDocument(b)
if len(da) != len(db) or len(pypdf.PdfReader(src).pages) != len(pypdf.PdfReader(out).pages): sys.exit("page count changed")
full = "".join(db[i].get_textpage().get_text_range() for i in range(len(db)))
if norm(new) not in norm(full): sys.exit("new text not found")
def lines(doc): return [norm(l) for i in range(len(doc)) for l in re.split(r"[\r\n]+", doc[i].get_textpage().get_text_range()) if l.strip()]
o = norm(old)
if any(o in l for l in lines(db)) and o not in norm(new):
    # allowed only if the same text legitimately appears more than once in the source
    if sum(l.count(o) for l in lines(da)) < 2: sys.exit("old text still present")
changed = [i for i in range(len(da)) if not (np.asarray(da[i].render(scale=.6).to_pil().convert("L")) == np.asarray(db[i].render(scale=.6).to_pil().convert("L"))).all()]
if len(changed) != 1: sys.exit(f"expected exactly one changed page, got {changed}")
print("OK")
