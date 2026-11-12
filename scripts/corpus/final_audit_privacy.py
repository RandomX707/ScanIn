#!/usr/bin/env python3
"""Pre-push privacy / secrets audit. Never prints matched content: only counts and file paths."""
import glob, io, json, re, subprocess, sys, zipfile
from pathlib import Path
import openpyxl, pypdfium2 as pdfium
from docx import Document

ROOT = Path(__file__).resolve().parents[2]
BASE = sys.argv[1] if len(sys.argv) > 1 else "1df628a"          # last commit before the stakeholder work
sh = lambda *a: subprocess.run(a, capture_output=True, text=True, cwd=ROOT).stdout

tracked = sh("git", "ls-files").splitlines()
print("tracked files:", len(tracked))
print("tracked under Policy Copies/ or .private/ or .runtime/:", sum(1 for t in tracked if t.startswith(("Policy Copies/", ".private/", ".runtime/"))))
print("tracked pgpass/secret/env files:", [t for t in tracked if re.search(r"pgpass|\.env$|secret|\.pem$|\.key$", t, re.I) and "node_modules" not in t])
print("ignored: Policy Copies", subprocess.run(["git", "check-ignore", "-q", "Policy Copies/RFQ.zip"], cwd=ROOT).returncode == 0,
      "| .private", subprocess.run(["git", "check-ignore", "-q", ".private/x"], cwd=ROOT).returncode == 0,
      "| .runtime", subprocess.run(["git", "check-ignore", "-q", ".runtime/dev/api.log"], cwd=ROOT).returncode == 0)

# ---- corpus text -> sensitive strings
texts = []
for f in glob.glob(str(ROOT / "Policy Copies/*.pdf")) + [str(ROOT / ".private/corpus/QCR/QCR/QCR Health.pdf")]:
    d = pdfium.PdfDocument(f)
    for i in range(len(d)): texts.append(d[i].get_textpage().get_text_range())
for f in glob.glob(str(ROOT / ".private/corpus/*/*/*.xlsx")) + glob.glob(str(ROOT / ".private/corpus/*/*/*.XLSX")):
    for ws in openpyxl.load_workbook(f).worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str): texts.append(c.value)
dd = Document(str(ROOT / ".private/corpus/QCR/QCR/Health Quote Format.docx"))
texts += [p.text for p in dd.paragraphs] + [c.text for t in dd.tables for r in t.rows for c in r.cells]
joined = "\n".join(texts)
lines = {re.sub(r"\s+", " ", l).strip() for l in re.split(r"[\r\n]+", joined)}
shingles = {l for l in lines if len(l) >= 28}
emails = set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", joined))
phones = set(re.findall(r"(?<!\d)(?:\+91[- ]?)?[6-9]\d{9}(?!\d)", joined))
ids = {m for m in re.findall(r"[A-Z0-9]{2,}[/-][A-Z0-9/-]{6,}", joined) if sum(c.isdigit() for c in m) >= 6}
longnums = set(re.findall(r"(?<!\d)\d{9,}(?!\d)", joined))
names = set()
for f in glob.glob(str(ROOT / ".private/outline/*.json")):
    for pg in json.load(open(f))["pages"]:
        for ln in pg["lines"]:
            m = re.match(r"^(?:Name of the Insured|Insured Name|Insured|Proposer)\s*[:：]\s*(.{4,60})$", ln["t"], re.I)
            if m: names.add(m.group(1).strip())
            m = re.match(r"^(?:Mr\.?|Mrs\.?|Ms\.?|Dr\.?)\s+[A-Z][A-Za-z .]{3,40}", ln["t"])
            if m: names.add(m.group(0).strip())
needles = {"verbatim line": shingles, "email": emails, "phone": phones, "policy-like id": ids, "long number": longnums, "person/insured name": names}
print({k: len(v) for k, v in needles.items()})

# ---- search tracked files (working tree) and every commit since BASE (added lines + messages)
blob = []
binary_skipped = []
for t in tracked:
    p = ROOT / t
    try:
        if p.stat().st_size > 6_000_000 or t.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".xlsx", ".docx", ".woff", ".woff2", ".zip")):
            binary_skipped.append(t); continue
        blob.append((t, p.read_text(errors="ignore")))
    except OSError:
        pass
hist = sh("git", "log", f"{BASE}..HEAD", "-p", "--no-color", "--text") + "\n" + sh("git", "log", f"{BASE}..HEAD", "--format=%B")
hits = {}
for kind, items in needles.items():
    for needle in items:
        for t, text in blob:
            if needle in text: hits.setdefault((kind, t), 0); hits[(kind, t)] += 1
        if needle in hist: hits.setdefault((kind, "<git history since base>"), 0); hits[(kind, "<git history since base>")] += 1
print("HITS (kind, location, count):", sorted((k, loc, n) for (k, loc), n in hits.items()) or "none")
print("binary/image tracked files not text-scanned:", len(binary_skipped))
added_bin = [l for l in sh("git", "diff", "--name-status", f"{BASE}..HEAD").splitlines() if re.search(r"\.(png|jpe?g|gif|webp|pdf|xlsx|docx|zip)$", l, re.I)]
print("binary files added/changed since base:", added_bin or "none")

# ---- secrets in added lines and tracked files
patterns = {"private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "jwt": r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}",
            "db url with password": r"postgres(?:ql)?(?:\+\w+)?://[^/\s:@]+:[^@\s]{3,}@", "PGPASSWORD": r"PGPASSWORD\s*=\s*\S+", "aws key": r"AKIA[0-9A-Z]{16}",
            "hex secret (64)": r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", "password assign": r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"}
added = "\n".join(l[1:] for l in hist.splitlines() if l.startswith("+") and not l.startswith("+++"))
for name, pat in patterns.items():
    n_hist = len(re.findall(pat, added)); n_tree = sum(len(re.findall(pat, text)) for _, text in blob)
    print(f"secret scan [{name}]: added-lines {n_hist}, tracked-tree {n_tree}")
# sha256 hex are expected (hashes in manifests/tests); list files with 64-hex in added lines for a human check
hexfiles = sorted({t for t, text in blob if re.search(patterns['hex secret (64)'], text)})
print("files containing 64-hex strings (hashes expected):", hexfiles[:12], "…" if len(hexfiles) > 12 else "")
# stakeholder FILE NAMES in committed files (informational: names are not personal data but identify the source)
names_in = sorted({t for t, text in blob if re.search(r"Policy copy|Quote Format|QCR Health|RFQ1?\.xlsx|Motor  Policy", text)})
print("committed files mentioning stakeholder file names:", names_in)

def _lines(h):
    cur = None
    for l in h.splitlines():
        if l.startswith('diff --git'): cur = l.split(' b/')[-1]
        yield cur, l


# ---- triage of hits WITHOUT printing content: masked form, length, pre-existing at base?, corpus frequency
def mask(s): return s[:2] + "*" * max(len(s) - 4, 1) + s[-2:] if len(s) > 6 else "*" * len(s)
for (kind, loc), n in sorted(hits.items()):
    for needle in needles[kind]:
        in_loc = (needle in hist) if loc.startswith("<git") else any(needle in text for t, text in blob if t == loc)
        if not in_loc: continue
        base_text = sh("git", "show", f"{BASE}:{loc}") if not loc.startswith("<git") else ""
        added_in = sorted({cur for cur, line in ((c, l) for c, l in _lines(hist)) if line.startswith("+") and needle in line}) if loc.startswith("<git") else []
        print("TRIAGE", kind, loc, "| masked:", mask(needle), "| len", len(needle), "| existed at base:", needle in base_text,
              "| occurrences in corpus:", joined.count(needle), "| added in:", added_in)
