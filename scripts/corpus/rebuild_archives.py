#!/usr/bin/env python3
"""Rebuild QCR.zip / RFQ.zip from the retained extraction (.private/corpus) using the exact entry paths in the inventory manifest.
Used only when the ORIGINAL archives are no longer present; the result is logically equivalent (names, paths, bytes of every entry),
not byte-identical containers, and every report that uses it must say 'reconstructed'."""
import json, sys, zipfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / ".private/fixtures/rebuilt-archives"; OUT.mkdir(parents=True, exist_ok=True)
manifest = json.load(open(ROOT / ".private/inventory/manifest.json"))
for archive in ("QCR.zip", "RFQ.zip"):
    entries = [o for o in manifest["occurrences"] if o["source"] == archive]
    with zipfile.ZipFile(OUT / archive, "w", zipfile.ZIP_DEFLATED) as z:
        for o in entries:
            src = ROOT / ".private/corpus" / archive.removesuffix(".zip") / o["path"]
            if not src.is_file(): sys.exit(f"missing retained extraction for an entry of {archive}")
            z.write(src, o["path"])
    print(archive, len(entries), "entries ->", OUT / archive)
