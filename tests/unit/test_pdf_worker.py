from __future__ import annotations

import hashlib
import io
import sys

import pytest
from reportlab.pdfgen import canvas

from packages.core.adapters.pdf.adapter import PdfAdapter
from packages.core.models.contracts import Patch, TypedValue
from packages.core.planning.planner import build_patch_plan
from packages.workers.parser import PdfVerificationWorkerUnavailable, RestrictedParser


def _form_pdf() -> bytes:
    output = io.BytesIO()
    pdf = canvas.Canvas(output)
    pdf.acroForm.textfield(name="claimant", x=40, y=700, width=220, height=24,
                           value="Ada", forceBorder=True)
    pdf.showPage()
    pdf.save()
    return output.getvalue()


def test_pdf_verification_runs_in_restricted_worker_and_returns_verified_report():
    source = _form_pdf()
    adapter = PdfAdapter()
    target = adapter.targets(source)[0]
    patch = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value,
                  target.fingerprint, TypedValue("string", "Adele"))
    plan = build_patch_plan(adapter, source, [patch], provenance={"edit_set_id": "worker-test"})
    candidate = adapter.apply(source, plan)

    report = RestrictedParser(".").verify_pdf(source, candidate, plan)

    assert report.outcome == "VERIFIED", report.as_dict()
    assert report.check("visual").status == "PASS"


def test_pdf_worker_crash_is_contained_and_has_actionable_reason_code():
    parser = RestrictedParser(".")
    result = parser.runner.run([sys.executable, "-c", "import os; os.abort()"], timeout_seconds=5)

    assert result.returncode != 0
    with pytest.raises(PdfVerificationWorkerUnavailable) as exc:
        parser._decode_pdf_verification_result(result)
    assert exc.value.reason_code == "pdf_verifier_worker_failed"
