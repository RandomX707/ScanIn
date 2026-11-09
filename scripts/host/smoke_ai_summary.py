#!/usr/bin/env python3
"""Explicit synthetic-only live API smoke test; never prints source or AI text."""
from __future__ import annotations

import argparse
from urllib.parse import urlsplit
from uuid import uuid4

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send-synthetic-content", action="store_true",
                        help="required opt-in: upload synthetic CSV content and request one external explanation")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    parsed = urlsplit(args.api)
    if not args.send_synthetic_content:
        parser.error("--send-synthetic-content is required to make the explicit live request")
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port not in {None, 8000}:
        parser.error("the smoke test only supports the local Channel360 API on loopback port 8000")
    base = args.api.rstrip("/")
    filename = f"channel360-ai-smoke-{uuid4()}.csv"
    content = ("claim_number,policyholder,loss_date\n"
               "SYNTHETIC-ONLY-001,Example Person,2026-01-15\n").encode("utf-8")
    with httpx.Client(base_url=base, timeout=30, trust_env=False, follow_redirects=False) as client:
        session = client.post("/api/local-demo/session", json={})
        if not session.is_success:
            raise SystemExit(f"Local demo identity unavailable (HTTP {session.status_code}).")
        identity = session.json()
        tenant_label = str(identity.get("tenant_name", "")).casefold()
        if "e2e" not in tenant_label and "test" not in tenant_label:
            raise SystemExit("Refusing to write smoke data outside a test/E2E-labeled local demo tenant.")
        token = identity.get("access_token")
        if not isinstance(token, str) or not token:
            raise SystemExit("Local demo identity response was invalid.")
        headers = {"Authorization": f"Bearer {token}"}
        upload = client.put(f"/api/documents?filename={filename}", content=content,
                            headers={**headers, "Content-Type": "text/csv"})
        if not upload.is_success:
            raise SystemExit(f"Synthetic upload failed (HTTP {upload.status_code}).")
        document_id = upload.json().get("document_id")
        if not isinstance(document_id, str):
            raise SystemExit("Synthetic upload response did not include a document identifier.")
        inspect = client.post(f"/api/documents/{document_id}/inspect?include_targets=true", headers=headers)
        if not inspect.is_success:
            raise SystemExit(f"Synthetic inspection failed (HTTP {inspect.status_code}).")
        versions = client.get(f"/api/documents/{document_id}/versions", headers=headers)
        if not versions.is_success:
            raise SystemExit(f"Synthetic version lookup failed (HTTP {versions.status_code}).")
        original = next((item for item in versions.json().get("items", []) if item.get("kind") == "original"), None)
        if not original:
            raise SystemExit("The synthetic immutable source version was unavailable.")
        result = client.post(f"/api/documents/{document_id}/versions/{original['version_id']}/ai-summary",
                             headers=headers)
        if not result.is_success:
            raise SystemExit(f"AI explanation failed (HTTP {result.status_code}); response content was suppressed.")
        body = result.json()
        if (body.get("generation_method") != "ai" or body.get("source_document_id") != document_id or
                body.get("source_version_id") != original["version_id"]):
            raise SystemExit("AI response did not match the synthetic source version.")
        print(f"PASS: synthetic AI explanation generated; provider={body.get('provider')}; "
              f"model={body.get('model')}; evidence_references={len(body.get('evidence_references', []))}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
