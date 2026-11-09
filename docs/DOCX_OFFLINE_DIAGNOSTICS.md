# DOCX Offline Candidate Diagnostics

Updated 2026-11-09. These tools inspect synthetic or otherwise authorized documents offline. They do not perform Channel360 approval, verification, registration, or release, and cannot authorize a download.

## Reproduce the supplied pair

The exact synthetic regression pair is in `tests/fixtures/docx/manual_validation/`. Its SHA-256 hashes are pinned in `expected-changes.json`:

- Original: `ad29bde1f5f30840888f28d44f37bdaf7eae06807fa6d176700d4723ef65aa9f`
- Candidate: `50ce49d2fd069e0f12524041ce9b63ff130c6864babe7aa35643427c803e8afb`

The candidate changes `Policyholder: Ada Example` to `Policyholder: Alex Example` and `CLM-DEMO-360` to `CLM-DEMO-361` using DocxAdapter and a frozen PatchPlan. It remains explicitly `candidate-UNVERIFIED.docx` in ignored `.runtime` storage. The application gate remains unchanged.

Run the structural diagnostic on the user-supplied runtime pair:

```sh
.venv/bin/python scripts/validate_docx_candidate.py \
  --original .runtime/docx-manual-validation/original.docx \
  --candidate .runtime/docx-manual-validation/candidate-UNVERIFIED.docx \
  --expected .runtime/docx-manual-validation/expected-changes.json \
  --json-out .runtime/docx-manual-validation/validation-report.json \
  --markdown-out .runtime/docx-manual-validation/validation-report.md
```

For this pair the local result is **structural PASS, visual NOT_VERIFIED, overall NOT_VERIFIED** (CLI exit code `3`). The JSON report records the supplied Linux/LibreOffice 25.2.3.2 result as `USER_SUPPLIED_NOT_REPRODUCED`; it is not treated as local rendering or Channel360 release evidence. The local structural checks reproduce the hashes, target values and fingerprints, package member count/order, permitted part changes, canonical OOXML restoration, styles/media preservation and provenance. Exit `0` is reserved for completed structural and supplied-PDF visual comparison, exit `1` means a structural/visual check failed, and exit `3` means the critical visual check is not verified.

The CLI may also compare two rendered PDF files with `--rendered-original-pdf` and `--rendered-candidate-pdf`. The caller must produce those files using an explicitly approved isolated renderer; the CLI does not launch arbitrary document renderers. It compares page count, target text geometry and PDFium-rendered pixels outside edit regions. Supplied render-file origin is not independently authenticated by the CLI.

## Regression coverage

`tests/unit/test_docx_candidate.py` builds synthetic original/candidate pairs through DocxAdapter and PatchPlan for body, table, header, footer, longer replacement, multipage, formatting, multiple tables, embedded image and repeated-value targets. Negative cases cover stale fingerprints, unexpected body XML changes, target-hash mismatch, multi-run spans, protected documents and tracked changes. `tests/unit/test_docx_adapter.py` exercises wrapped text, changed downstream positions, page count, non-text borders, image pixels and spacing changes. All layout tests use synthetic PDFs; they verify comparator behavior, not an installed Word renderer.

## Current rendering boundary

This host has LibreOffice 26.8.2.2 installed. The existing macOS Seatbelt profile blocks the ordinary invocation before launch (`sandbox_apply: Operation not permitted`); prior host-authorized diagnostic launches crashed at `macxp_resolveAlias → osl::realpath → osl_setCommandArgs`. A constrained probe denied both outside-tree reads and loopback network access. No Podman, Docker, Colima, Lima, QEMU or other Linux worker runtime is installed. LibreOffice is not run unsandboxed and M6 remains incomplete.

The smallest practical automated option is one isolated Linux rendering worker, either on an existing Linux host or inside a small VM on macOS. A starting resource envelope to benchmark is a 4 GiB VM with one serialized conversion at a time, a 2 GiB per-job memory limit, 1–2 CPU quota, 512 MiB scratch/output quota and a 60-second timeout. These are proposed initial limits, not measured guarantees. The worker should run rootless, deny networking, use a read-only image/root filesystem, mount each source read-only, write only to per-job scratch/output, drop capabilities, set `no-new-privileges`, retain the default seccomp filter, and terminate the complete process group on timeout. Podman documents `--network=none` and `--read-only`; its rootless and resource-limit behavior must still be validated on the selected Linux/cgroup version ([Podman run reference](https://docs.podman.io/en/latest/markdown/podman-run.1.html), [Docker rootless mode](https://docs.docker.com/engine/security/rootless/), [Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)). A container is an isolation layer that still requires host-kernel and runtime review; passing a conversion alone does not establish renderer safety.

Until that worker is available and its isolation, rendering, pixel comparisons, reflow rules, and full approval/download path pass, DOCX native release remains blocked.
