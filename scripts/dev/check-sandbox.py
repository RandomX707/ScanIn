#!/usr/bin/env python3
"""Deployment preflight for the document-processing confinement and its external dependencies. Exit 0 only when every feature that needs
confinement is available AND verified on this host. Linux/other hosts without a passing self-test are reported NOT VERIFIED (never assumed)."""
from __future__ import annotations
import os, re, shutil, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from packages.workers import sandbox as sb  # noqa: E402

def run(cmd):
    try: return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip().splitlines()[0]
    except Exception: return ""

problems: list[str] = []
status = sb.sandbox_status(refresh=True)
print(f"platform        : {status.platform}")
print(f"confinement     : {status.runner_label}")
print(f"self-test       : {'PASSED' if status.verified_here else 'NOT VERIFIED'} — {status.detail}")
if not status.verified_here:
    problems.append("confinement not verified on this host: page previews and signature validation stay disabled (fail closed)")
if sys.platform.startswith("linux") and not shutil.which("bwrap"):
    print("                  install bubblewrap (apt install bubblewrap) and run this check again; Linux support is NOT VERIFIED until then")
soffice, openssl = sb.resolve_tool("soffice"), sb.resolve_tool("openssl")
print(f"LibreOffice     : {'found ' + soffice.path + ' (' + run([soffice.path, '--version']) + ')' if soffice else 'MISSING (DOCX page previews disabled)'}")
print(f"OpenSSL (cms)   : {'found ' + openssl.path + ' (' + run([openssl.path, 'version']) + ')' if openssl else 'MISSING or LibreSSL only (signature validation disabled)'}")
if not soffice: problems.append("LibreOffice not found (set IDW_SOFFICE_PATH)")
if not openssl: problems.append("OpenSSL 3+ not found (set IDW_OPENSSL_PATH)")
fonts = {"Carlito (Calibri)": ("carlito",), "Liberation Sans (Arial)": ("liberationsans", "arimo"), "Liberation Serif (Times New Roman)": ("liberationserif", "tinos"), "Caladea (Cambria)": ("caladea",)}
roots = [Path(p) for p in ("/usr/share/fonts", "/usr/local/share/fonts", "/Library/Fonts", "/System/Library/Fonts", str(Path.home() / "Library/Fonts"))]
if soffice and ".app/" in soffice.path: roots.append(Path(soffice.path).parents[1] / "Resources/fonts")
have = " ".join(str(p).lower() for r in roots if r.exists() for p in r.rglob("*") if p.suffix.lower() in (".ttf", ".otf", ".ttc"))
for label, keys in fonts.items():
    ok = any(k in have.replace("-", "").replace(" ", "") for k in keys)
    print(f"font            : {label:36} {'present' if ok else 'MISSING (layout will differ from Word)'}")
    if not ok: print("                  (documents naming this font will be re-flowed; the page preview discloses replaced fonts)")
print("trust policy    :", os.environ.get("IDW_PDF_TRUST_CAFILE") or "none configured (every signer reports 'trust not established')")
print("\nRESULT:", "READY" if not problems else "NOT READY — " + "; ".join(problems))
sys.exit(0 if not problems else 1)
