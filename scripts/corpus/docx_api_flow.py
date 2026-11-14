"""Edit one real DOCX via the API (fresh tenant) and compare the saved package with the source. Metrics only."""
import hashlib, io, json, os, re, subprocess, sys, urllib.parse, urllib.request, zipfile, collections
from pathlib import Path
from lxml import etree
ROOT = Path(__file__).resolve().parents[2]
API = os.environ.get("CHANNEL360_API", "http://127.0.0.1:8010")
tok = json.loads(subprocess.run([sys.executable, str(ROOT / "scripts/dev/issue_iso_token.py")], capture_output=True, text=True, check=True).stdout)
H = {"Authorization": f"Bearer {tok['token']}"}
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

def call(method, path, body=None, headers=None):
    req = urllib.request.Request(API + path, data=body, method=method, headers={**H, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=300) as r: return r.status, r.read()
    except urllib.error.HTTPError as e: return e.code, e.read()

def stats(data: bytes):
    z = zipfile.ZipFile(io.BytesIO(data)); xml = z.read("word/document.xml").decode("utf8")
    rels = z.read("word/_rels/document.xml.rels").decode()
    return {"paras": len(re.findall(r"<w:p[ >]", xml)), "tables": len(re.findall(r"<w:tbl>", xml)), "rows": len(re.findall(r"<w:tr[ >]", xml)),
            "drawings": len(re.findall(r"<w:drawing>", xml)), "ext_links": len(re.findall(r'TargetMode="External"', rels)),
            "hyperlinks": len(re.findall(r"<w:hyperlink\b", xml)), "ins": len(re.findall(r"<w:ins\b", xml)), "del": len(re.findall(r"<w:del\b", xml)),
            "track_setting": b"trackRevisions" in z.read("word/settings.xml"), "parts": len(z.namelist()),
            "media": sorted(n for n in z.namelist() if "media" in n)}

def run(path: Path):
    data = path.read_bytes(); sha = hashlib.sha256(data).hexdigest()
    s, b = call("PUT", "/api/documents?filename=" + urllib.parse.quote(path.name), data, {"Content-Type": "application/octet-stream"}); reg = json.loads(b)
    d, v = reg["document_id"], reg["version_id"]; call("POST", f"/api/documents/{d}/inspect")
    s, b = call("GET", f"/api/documents/{d}/versions/{v}/preview"); pv = json.loads(b)
    blocks = pv.get("blocks", [])
    print("mode", pv["edit"], "blocks", len(blocks), "editable", sum(1 for x in blocks if x["e"]), "reasons", dict(collections.Counter(x["r"] for x in blocks if not x["e"])))
    ed = [x for x in blocks if x["e"] and x["text"] and len(x["text"]) > 12]
    if not ed: print("no editable block"); return
    blk = ed[len(ed) // 2]
    s, b = call("POST", f"/api/documents/{d}/versions/{v}/save", json.dumps({"changes": [{"target_id": blk["target_id"], "fingerprint": blk["fp"], "value": blk["text"] + " e2e"}]}).encode(), {"Content-Type": "application/json"})
    res = json.loads(b)
    if s != 200: print("SAVE", s, res.get("code"), (res.get("message") or "")[:120]); return
    s1, new = call("GET", f"/api/documents/{d}/versions/{res['version_id']}/file"); s2, old = call("GET", f"/api/documents/{d}/versions/{v}/file")
    a, c = stats(data), stats(new)
    print("saved", res.get("outcome"), "orig_sha_ok", hashlib.sha256(old).hexdigest() == sha)
    print("before", {k: v for k, v in a.items() if k != "media"}); print("after ", {k: v for k, v in c.items() if k != "media"}); print("media same:", a["media"] == c["media"])
    za, zn = zipfile.ZipFile(io.BytesIO(data)), zipfile.ZipFile(io.BytesIO(new))
    print("changed parts:", [n for n in za.namelist() if n in zn.namelist() and za.read(n) != zn.read(n)], "added:", sorted(set(zn.namelist()) - set(za.namelist())), "removed:", sorted(set(za.namelist()) - set(zn.namelist())))
    import docx; d2 = docx.Document(io.BytesIO(new)); print("python-docx reopen ok:", len(d2.paragraphs), "paragraphs,", len(d2.tables), "table")

if __name__ == "__main__":
    run(Path(sys.argv[1]))
