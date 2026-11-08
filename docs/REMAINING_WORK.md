# Remaining Work

Updated 2026-11-08. Do not implement M4–M7 as part of the current UI stabilization baseline.

## Product and operational work

1. Implement a durable Redis/Valkey queue, bounded retries, job recovery and authenticated processing events.
2. Validate OS-level parser network isolation on each supported host and document the resulting platform boundary.
3. Expand the synthetic/licensed acceptance corpus to the planned 50 representative documents.
4. Replace local demo identities with a production identity provider before deployment; add key rotation and operational secret management.
5. Replace development filesystem storage with a production object store and define backup, retention and recovery controls.
6. Complete production threat modeling, deployment hardening, observability, incident response and accessibility review.

## Planned milestones

- **M4:** deterministic templates, field/cross-field rules, structural summaries and evidence navigation.
- **M5:** constrained unsigned AcroForm PDF editing; signed, certified and XFA documents stay read-only unless separately supported.
- **M6:** constrained DOCX run editing with explicit refusal of unsupported fields/structures.
- **M7:** optional OCR/Docling/LLM providers behind disabled-by-default interfaces; deterministic core remains usable without them.

Current evidence and limitations are in [README](../README.md), [feature matrix](FEATURE_COMPLETENESS_MATRIX.md) and [known limitations](KNOWN_LIMITATIONS.md).
