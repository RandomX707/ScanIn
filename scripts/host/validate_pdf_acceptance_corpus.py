#!/usr/bin/env python3
"""Build and verify a 50-file synthetic M5 PDF corpus (no database or uploads)."""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from create_browser_fixtures import (create_encrypted_pdf, create_restricted_pdf,
                                     create_scanned_pdf, create_xfa_pdf)
from packages.core.adapters.pdf.adapter import PdfAdapter
from packages.core.adapters.registry import AdapterRegistry
from packages.core.models.contracts import Patch, TypedValue
from packages.core.planning.planner import build_patch_plan
from packages.core.verification.pdf import PdfIndependentVerifier


DEFAULT_OUT = ROOT / ".runtime" / "pdf-acceptance-corpus"


def _reportlab_text_form(index: int) -> bytes:
    output = io.BytesIO()
    page_size = (612, 792) if index % 2 == 0 else (612, 842)
    pdf = canvas.Canvas(output, pagesize=page_size)
    pdf.setTitle(f"Synthetic M5 text form {index:03d}")
    pdf.drawString(38, page_size[1] - 42, f"Synthetic claim form {index:03d}")
    pdf.acroForm.textfield(name=f"claimant_{index:03d}", x=38, y=page_size[1] - 100,
                           width=180 + (index % 4) * 25, height=24,
                           value=f"Synthetic{index:03d}", maxlen=40,
                           fontName="Helvetica", fontSize=10 + index % 3)
    if index % 3 == 0:
        pdf.showPage()
        pdf.drawString(38, page_size[1] - 42, f"Synthetic continuation {index:03d}")
    pdf.save()
    return output.getvalue()


def _reportlab_controls(index: int) -> tuple[bytes, str, str, str]:
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=(612, 792))
    form = pdf.acroForm
    # The corpus toggles both states, so model these controls as optional rather
    # than inheriting ReportLab's default REQUIRED flag.
    form.checkbox(name=f"consent_{index:03d}", x=38, y=720, checked=bool(index % 2), fieldFlags="")
    form.radio(name=f"coverage_{index:03d}", value="basic", x=38, y=670, selected=True)
    form.radio(name=f"coverage_{index:03d}", value="extended", x=90, y=670)
    form.choice(name=f"region_{index:03d}", x=38, y=620, value="CA", options=["CA", "NY", "TX"],
                fontName="Helvetica", fontSize=12)
    pdf.showPage()
    pdf.save()
    field_type = ("checkbox", "radio", "choice")[index % 3]
    field_name = {"checkbox": f"consent_{index:03d}", "radio": f"coverage_{index:03d}",
                  "choice": f"region_{index:03d}"}[field_type]
    new_value = {"checkbox": TypedValue("boolean", not bool(index % 2)),
                 "radio": TypedValue("choice", "extended"),
                 "choice": TypedValue("choice", "NY")}[field_type]
    return output.getvalue(), field_type, f"pdf:{field_name}", json.dumps(new_value.canonical())


def build_corpus(output: Path) -> list[dict[str, Any]]:
    if not shutil.which("qpdf"):
        raise RuntimeError("qpdf is required for the M5 acceptance corpus")
    output.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []

    def add(filename: str, data: bytes, producer: str, expected: str,
            target_id: str | None = None, new_value: TypedValue | None = None) -> None:
        path = output / filename
        path.write_bytes(data)
        records.append({"filename": filename, "producer": producer, "expected_capability": expected,
                        "target_id": target_id,
                        "new_value": new_value.canonical() if new_value is not None else None,
                        "source_sha256": hashlib.sha256(data).hexdigest()})

    for index in range(1, 21):
        data = _reportlab_text_form(index)
        name = f"reportlab-text-{index:02d}"
        add(f"{name}.pdf", data, "ReportLab", "editable", f"pdf:claimant_{index:03d}",
            TypedValue("string", f"Changed{index:03d}"))

    for index in range(1, 11):
        data, field_type, target_id, _ = _reportlab_controls(index)
        value = {"checkbox": TypedValue("boolean", not bool(index % 2)),
                 "radio": TypedValue("choice", "extended"),
                 "choice": TypedValue("choice", "NY")}[field_type]
        add(f"reportlab-controls-{index:02d}.pdf", data, "ReportLab", "editable", target_id, value)

    for index in range(1, 11):
        name = f"qpdf-object-streams-{index:02d}"
        original_path = output / f"{name}-source.pdf"
        final_path = output / f"{name}.pdf"
        original_path.write_bytes(_reportlab_text_form(index + 100))
        subprocess.run(["qpdf", "--object-streams=generate", str(original_path), str(final_path)],
                       check=True, capture_output=True)
        original_path.unlink()
        add(final_path.name, final_path.read_bytes(), "qpdf (rewrote ReportLab form)", "editable",
            f"pdf:claimant_{index + 100:03d}", TypedValue("string", f"QpdfEdit{index:03d}"))

    restricted = [
        ("signed", create_restricted_pdf("signed"), "pypdf", "read_only"),
        ("certified", create_restricted_pdf("certified"), "pypdf", "read_only"),
        ("xfa", create_xfa_pdf(), "pypdf", "read_only"),
        ("encrypted", create_encrypted_pdf(), "pypdf", "rejected"),
        ("scanned", create_scanned_pdf(), "ReportLab + Pillow", "read_only"),
    ]
    for restriction, data, producer, expected in restricted:
        for index in range(1, 3):
            add(f"{restriction}-{index:02d}.pdf", data, producer, expected)

    if len(records) != 50:
        raise AssertionError(f"expected 50 corpus files, generated {len(records)}")
    manifest = {"corpus": "Channel360 M5 synthetic acceptance corpus", "version": 1,
                "documents": records}
    (output / "expected.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return records


def validate(output: Path) -> dict[str, Any]:
    records = build_corpus(output)
    registry, adapter, verifier = AdapterRegistry(), PdfAdapter(), PdfIndependentVerifier()
    results: list[dict[str, Any]] = []
    for record in records:
        source = (output / record["filename"]).read_bytes()
        if hashlib.sha256(source).hexdigest() != record["source_sha256"]:
            raise AssertionError(f"source fixture digest changed: {record['filename']}")
        selection = registry.inspect(source, filename=record["filename"])
        capability = selection.profile.level
        if capability != record["expected_capability"]:
            raise AssertionError(f"{record['filename']}: expected {record['expected_capability']}, got {capability}")
        row: dict[str, Any] = {"filename": record["filename"], "producer": record["producer"],
                               "capability": capability, "source_sha256": record["source_sha256"]}
        if capability == "editable":
            target = next((item for item in adapter.targets(source) if item.id == record["target_id"]), None)
            if target is None or not target.editable:
                raise AssertionError(f"{record['filename']}: expected editable target is unavailable")
            patch = Patch(record["source_sha256"], target.id, target.value, target.fingerprint,
                          TypedValue(**record["new_value"]))
            plan = build_patch_plan(adapter, source, [patch], provenance={"edit_set_id": record["filename"]})
            candidate = adapter.apply(source, plan)
            if not candidate.startswith(source):
                raise AssertionError(f"{record['filename']}: incremental prefix was not preserved")
            report = verifier.verify(source, candidate, plan)
            if report.outcome != "VERIFIED":
                raise AssertionError(f"{record['filename']}: candidate did not verify: {report.as_dict()}")
            updated = next(item for item in adapter.targets(candidate) if item.id == target.id)
            if updated.value != patch.new_value:
                raise AssertionError(f"{record['filename']}: field value did not round-trip")
            row["verification"] = report.as_dict()
        else:
            editable_targets = [item.id for item in adapter.targets(source) if item.editable] if capability == "read_only" and selection.profile.adapter == "pdf" else []
            if editable_targets:
                raise AssertionError(f"{record['filename']}: restricted document exposed editable targets")
            row["verification"] = "no native patch offered"
        row["status"] = "PASS"
        results.append(row)
    summary = {"total": len(results), "passed": len(results), "failed": 0,
               "editable": sum(item["capability"] == "editable" for item in results),
               "read_only": sum(item["capability"] == "read_only" for item in results),
               "rejected": sum(item["capability"] == "rejected" for item in results),
               "producers": sorted({item["producer"] for item in results}), "results": results}
    (output / "validation-results.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_OUT
    try:
        summary = validate(output)
    except Exception as exc:
        print(f"M5 PDF corpus: FAIL ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 1
    print(f"M5 PDF corpus: {summary['passed']}/{summary['total']} PASS; "
          f"editable={summary['editable']}, read_only={summary['read_only']}, rejected={summary['rejected']}; "
          f"producers={', '.join(summary['producers'])}")
    print(f"Manifest: {output / 'expected.json'}")
    print(f"Results: {output / 'validation-results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
