from __future__ import annotations

"""Exercise the live API workflow; this script never fabricates a success result."""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts" / "e2e-demo"
SOURCE = ARTIFACTS / "synthetic_motor_claim.csv"
SOURCE_BYTES = b"claim_number,policy_number,loss_amount\nCLM-1001,POL-9001,1250.00\n"


class DemoFailure(RuntimeError):
    def __init__(self, stage: str, message: str):
        self.stage = stage
        super().__init__(message)


def call(base: str, token: str, path: str, *, method: str = "GET", body: bytes | None = None,
         content_type: str = "application/json") -> tuple[dict | bytes, dict[str, str]]:
    request = urllib.request.Request(base.rstrip("/") + path, data=body, method=method,
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": content_type})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            headers = {key.lower(): value for key, value in response.headers.items()}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {detail[:1000]}") from None
    if "application/json" in headers.get("content-type", ""):
        return json.loads(raw), headers
    return raw, headers


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise DemoFailure("configuration", f"Set {name} in the shell; the value is never written to evidence.")
    return value


def record(evidence: dict, stage: str, data: dict) -> None:
    evidence["stages"].append({"stage": stage, "status": "PASS", "evidence": data})
    (ARTIFACTS / "workflow-evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")


def main() -> int:
    base = os.environ.get("IDW_API_BASE_URL", "http://127.0.0.1:8000")
    processor = require_env("IDW_DEMO_PROCESSOR_TOKEN")
    administrator = require_env("IDW_DEMO_ADMIN_TOKEN")
    reviewer = require_env("IDW_DEMO_REVIEWER_TOKEN")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    if SOURCE.exists() and SOURCE.read_bytes() != SOURCE_BYTES:
        raise DemoFailure("fixture", f"Refusing to overwrite a different file at {SOURCE}")
    SOURCE.write_bytes(SOURCE_BYTES)
    evidence: dict = {"api_base": base, "source_path": str(SOURCE.relative_to(ROOT)), "stages": []}
    current_stage = "upload"
    try:
        uploaded, _ = call(base, processor, f"/api/documents?filename={SOURCE.name}", method="PUT",
                           body=SOURCE_BYTES, content_type="text/csv")
        document_id, original_version = uploaded["document_id"], uploaded["version_id"]
        record(evidence, "upload", {"document_id": document_id, "original_version_id": original_version,
                                     "sha256": uploaded["sha256"], "state": uploaded["state"]})

        current_stage = "inspect"
        inspected, _ = call(base, processor, f"/api/documents/{document_id}/inspect?include_targets=true", method="POST")
        if inspected.get("state") != "ready" or inspected.get("adapter") != "csv":
            raise RuntimeError("The uploaded CSV did not reach verified CSV inspection readiness.")
        record(evidence, "inspect", {"state": inspected["state"], "adapter": inspected["adapter"],
                                     "capability": inspected["capability"], "targets": len(inspected["targets"])})

        current_stage = "view_and_select"
        viewed, _ = call(base, processor, f"/api/documents/{document_id}/versions/{original_version}/view")
        target = next(item for item in viewed["targets"] if item["id"] == "csv:2:1")
        record(evidence, "view_and_select", {"version_id": viewed["version_id"], "source_sha256": viewed["source_sha256"],
                                             "target_id": target["id"], "old_value": target["value"]})

        current_stage = "data_correction"
        categorized, _ = call(base, administrator, f"/api/documents/{document_id}/category", method="PUT",
                              body=json.dumps({"category": "insurer-authored"}).encode())
        field, _ = call(base, processor, f"/api/documents/{document_id}/fields", method="POST",
                        body=json.dumps({"target_id": target["id"], "fingerprint": target["fingerprint"]}).encode())
        correction, _ = call(base, processor, f"/api/documents/{document_id}/fields/{field['field_id']}/corrections",
                             method="POST", body=json.dumps({"corrected_value": {"type": "string", "value": "CLM-1003"},
                                                             "reason": "Synthetic demonstration correction"}).encode())
        record(evidence, "data_correction", {"category": categorized["category"], "field_id": field["field_id"],
                                              "correction_id": correction["id"], "source_unchanged": True})

        current_stage = "preflight_and_freeze"
        draft, _ = call(base, processor, f"/api/documents/{document_id}/edit-sets", method="POST",
                        body=json.dumps({"reason": "Synthetic demonstration document correction"}).encode())
        current_stage = "unauthorized_edit_rejection"
        try:
            call(base, reviewer, f"/api/documents/{document_id}/edit-sets/{draft['edit_set_id']}/patches",
                 method="PUT", body=json.dumps({"patches": [{"target_id": target["id"],
                     "fingerprint": target["fingerprint"], "new_value": {"type": "string", "value": "forbidden"}}]}).encode())
        except RuntimeError as error:
            if not str(error).startswith("HTTP 403:"):
                raise
            record(evidence, "unauthorized_edit_rejection", {"status": 403, "rejected_role": "reviewer"})
        else:
            raise RuntimeError("A reviewer identity unexpectedly received processor edit permission.")
        current_stage = "preflight_and_freeze"
        patch, _ = call(base, processor, f"/api/documents/{document_id}/edit-sets/{draft['edit_set_id']}/patches",
                        method="PUT", body=json.dumps({"patches": [{"target_id": target["id"],
                            "fingerprint": target["fingerprint"], "new_value": {"type": "string", "value": "CLM-1002"}}]}).encode())
        preflight, _ = call(base, processor, f"/api/documents/{document_id}/edit-sets/{draft['edit_set_id']}/preflight", method="POST")
        record(evidence, "preflight_and_freeze", {"edit_set_id": draft["edit_set_id"], "status": preflight["status"],
                                                   "frozen_hash": preflight["frozen_hash"], "patches": len(patch["patches"])})

        current_stage = "review"
        review, _ = call(base, reviewer, f"/api/documents/{document_id}/edit-sets/{draft['edit_set_id']}/review",
                         method="POST", body=json.dumps({"decision": "approved", "note": "Synthetic demo review"}).encode())
        record(evidence, "review", review)

        current_stage = "independent_verify_and_release"
        released, _ = call(base, processor, f"/api/documents/{document_id}/edit-sets/{draft['edit_set_id']}/release", method="POST")
        record(evidence, "independent_verify_and_release", {"outcome": released["outcome"],
                                                              "version_id": released["version_id"],
                                                              "download_available": released["download_available"],
                                                              "verification": released["verification"]})
        if released["outcome"] != "VERIFIED" or not released["download_available"]:
            raise RuntimeError("Release was not VERIFIED; no downloaded output will be reported as successful.")
        current_stage = "download_and_lineage"
        output, _ = call(base, processor, f"/api/documents/{document_id}/versions/{released['version_id']}/download")
        if not isinstance(output, bytes) or b"CLM-1002" not in output or b"CLM-1001" in output:
            raise RuntimeError("Downloaded contents did not match the approved change.")
        output_path = ARTIFACTS / "verified_motor_claim_derived.csv"
        output_path.write_bytes(output)
        current_stage = "tampered_candidate_rejection"
        from packages.core.adapters.csv_adapter.adapter import CsvAdapter
        from packages.core.models.contracts import Patch, TypedValue
        from packages.core.planning.planner import build_patch_plan
        from packages.core.verification.text_adapters import CsvIndependentVerifier

        adapter = CsvAdapter()
        original_target = next((item for item in adapter.targets(SOURCE_BYTES) if item.id == target["id"]), None)
        if original_target is None:
            raise RuntimeError("The original CSV target no longer resolves.")
        frozen = build_patch_plan(adapter, SOURCE_BYTES, [Patch(
            source_version_id=viewed["source_sha256"], target_id=target["id"],
            expected_old_value=original_target.value, expected_fingerprint=target["fingerprint"],
            new_value=TypedValue("string", "CLM-1002"),
        )], provenance={"edit_set_id": draft["edit_set_id"], "document_id": document_id,
                       "base_version_id": original_version, "actor_id": "demo-processor"})
        tampered = output.replace(b"CLM-1002", b"CLM-9999", 1)
        tampered_report = CsvIndependentVerifier().verify(SOURCE_BYTES, tampered, frozen)
        if tampered_report.outcome != "BLOCKED":
            raise RuntimeError("The independent verifier did not block an out-of-plan candidate mutation.")
        record(evidence, "tampered_candidate_rejection", {"outcome": tampered_report.outcome,
            "failed_checks": [check.check for check in tampered_report.checks if check.status == "FAIL"]})
        current_stage = "download_and_lineage"
        history, _ = call(base, processor, f"/api/documents/{document_id}/versions")
        audit, _ = call(base, processor, f"/api/documents/{document_id}/audit")
        original, _ = call(base, processor, f"/api/documents/{document_id}/original")
        if original != SOURCE_BYTES:
            raise RuntimeError("Immutable original bytes changed during the workflow.")
        record(evidence, "download_and_lineage", {"derived_path": str(output_path.relative_to(ROOT)),
            "original_byte_identical": True, "versions": history["items"],
            "audit_sequences": [entry["sequence"] for entry in audit["items"]]})
        print(json.dumps(evidence, indent=2, sort_keys=True))
        return 0
    except Exception as error:
        evidence["failure"] = {"stage": current_stage,
                                "error_type": type(error).__name__, "message": str(error)}
        (ARTIFACTS / "workflow-evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        print(json.dumps(evidence, indent=2, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
