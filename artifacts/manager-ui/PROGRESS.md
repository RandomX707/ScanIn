# Manager UI progress

Prototype used: docs/design/ScanIn_Channel360_Original_Alignment_Fixed.html (no manager-prototype.html exists; confirmed with the user).
Environment note: clamd was not running (stale socket); started /opt/homebrew/sbin/clamd in the background.

- M1 done: Prototype CSS and layout shell (commit 907f443)
- Push blocked: no GitHub credentials in this environment (git push fails with 'Invalid username or token'; gh not logged in). Commits are local on feat/manager-ui; run `git push -u origin feat/manager-ui` after `gh auth login`.
- M2 done: Workspace (Original, Chunks, map, pulse), /chunks and /preview endpoints (commit ed3959f)
- M3 done: POST /versions/{v}/save one-call flow, same-format text writer, demo v2/v3 (commit a40efe0)
- M4 done: Edits tab per format (text, CSV/XLSX grid, DOCX, PDF forms, read-only states); 18 Playwright flows pass (commit dc06416)
- M5 done: classification, drafts, patches, preflight, release, sidecars, capability panels, audit, AI, identity and paging controls are absent from the UI (grep-verified); backend routes/tables untouched; owner uploads authorised server-side in the save flow (commit 9fc7765)
- M6 done: Playwright 18/18, backend 559 passed/32 skipped, side-by-side screenshots + DIFFERENCES.md, README status (commit M6)
