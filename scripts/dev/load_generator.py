"""Synthetic contention for repeatability runs against the ISOLATED stack only.
  --cpu N        N busy-loop processes
  --uploads N    N threads that upload/inspect/render/compare real-sized documents in a loop (ClamAV, sandboxes, LibreOffice)
Runs for --seconds, then stops. Never talks to the shared application."""
from __future__ import annotations
import argparse, multiprocessing as mp, sys, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from iso_client import Client  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

def burn(stop):
    x = 0
    while time.time() < stop:
        x = (x * 31 + 7) % 1000003

def hammer(stop, results, i):
    c = Client(); docx = ROOT / "Policy Copies/QCR/Health Quote Format.docx"; pdf = ROOT / "Policy Copies/CAR Policy copy.pdf"
    n = 0
    while time.time() < stop:
        for f in (docx, pdf):
            s, reg, insp = c.upload(f)
            if s == 200 and reg:
                d, v = reg["document_id"], reg["version_id"]
                if f is docx: c.call("GET", f"/api/documents/{d}/versions/{v}/render", raw=True)
                c.json(f"/api/documents/{d}/versions/{v}/chunks"); n += 1
    results[i] = n

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--cpu", type=int, default=0); ap.add_argument("--uploads", type=int, default=0); ap.add_argument("--seconds", type=int, default=120)
    a = ap.parse_args(); stop = time.time() + a.seconds
    procs = [mp.Process(target=burn, args=(stop,)) for _ in range(a.cpu)]
    [p.start() for p in procs]
    results = {}; threads = [threading.Thread(target=hammer, args=(stop, results, i)) for i in range(a.uploads)]
    [t.start() for t in threads]; [t.join() for t in threads]; [p.join() for p in procs]
    print("load finished; document round-trips:", sum(results.values()))
