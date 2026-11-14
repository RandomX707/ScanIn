# Channel360 document-experience upgrade: release readiness review

Pull request: https://github.com/RandomX707/ScanIn/pull/2 (`feat/manager-ui` into `main`, **not merged**).
Verified code revision: the pull-request head at the time of review (`git rev-parse HEAD` on `feat/manager-ui`; the verified commit before this report was `9fceb70fd5f622cd17cee9237bbec9e398033a85`). Evidence in this report is counts and verdicts only: no stakeholder content, credentials or host-specific secrets. Sensitive operational detail (backup location, credentials, role dumps) is kept in a mode-700 backup directory outside the repository and in `.private/`.

**Final recommendation: READY FOR CONTROLLED MACOS DEPLOYMENT**, subject to the approvals, the write-quiesced window and the open items in section 16. It is **not** ready for Linux or container deployment.

## 1. Branch and merge state

`main` contained PR #1, a squash of an earlier slice of this branch. Its tree is byte-identical to commit `55159a7`, which is an ancestor of this branch (verified by tree comparison). Without action the pull request would have conflicted in 14 files. A content-preserving `-s ours` merge (`12b29e5`) records `main` as integrated; the branch tree is unchanged by it and `git merge-tree` against `main` is clean. **Merge with a merge commit or squash; do not rebase** (a rebase would replay 79 commits onto a squash of themselves and re-create the conflicts).

## 2. Independent code review findings

| # | Area | Finding | Severity | Status |
|---|---|---|---|---|
| R1 | Sandbox / verification | `packages/workers/office.py` (DOCX visual-layout verification, runs in the API process) used a private Seatbelt profile that cannot start LibreOffice on current macOS. Every `structural_and_visual` verification ended BLOCKED. It fails closed, so it is a functional defect, not a hole. Reproduced live (`office_render_failed`). | Medium (release-affecting when the visual mode is enabled) | **Fixed**: delegates to the single verified confinement; regression test |
| R2 | Sandbox | LibreOffice was granted `mach*`. | Low (documented residual risk) | **Narrowed** to `mach-lookup`; rendering re-verified |
| R3 | Migration 0008 | No `lock_timeout`: a long application transaction on `document_versions` or `audit_events` would make the migration wait while every new query queued behind its pending `ACCESS EXCLUSIVE` lock. | Medium | **Fixed**: `SET LOCAL lock_timeout='30s'`; failure rehearsed (section 9) |
| R4 | Compare cache | Cache key is `(tenant, base sha256, target sha256)` and requests first prove both versions belong to the caller's tenant and document, so results cannot be served across tenants or against a different file. Bounded to 8 entries and 32 MB. | none | verified by review and tests |
| R5 | Sandbox | Suspected unconfined `qpdf --check` on document-derived bytes. | **refuted** | Production verification runs inside the restricted PDF worker, which `qpdf` inherits; preflight (the only in-process use) builds a plan and never verifies a candidate. A change I made to confine it again broke the worker (nested Seatbelt is impossible) and was **reverted** (`9fceb70`). No code change remains. |
| R6 | Environment | The shared frontend (`:5174`) is a Vite dev server rooted in this working tree, so it serves whatever source is checked out, independent of the shared backend, which runs from an exported copy of `1df628a`. Checking out the new branch already changes what the shared UI serves. | Operational risk | **Not changed** (shared services untouched). Runbook step: deploy from a dedicated checkout, not from the development worktree. |
| R7 | Isolation | Isolated databases live in the same PostgreSQL cluster and reuse the same role passwords as the shared database (isolation is by database name, per-database pgpass, per-database storage, loopback and port validators). They are not separate credentials. | Low | Documented residual risk |

Review also covered, with no confirmed defect: windowed-index path handling (version id plus content hash, worksheet resolved through the metadata, no path from request data), window and cell endpoints (bounded size, tenant-scoped version lookup), download eligibility (security flow 23/23), signed-PDF working copies (all signature structures removed and checked, permanent marking strip, owner-only creation, lineage persisted), file upload validation, tenant isolation, and error handling. Defects fixed earlier on this branch and re-verified in this review: migration 0008 backfill was a silent no-op under `FORCE ROW LEVEL SECURITY`; cell marks never resolved because the bounded cache write and read used different stores; the stack script clobbered `$1`; a missing stored object caused an unhandled 500 (now a controlled 409).

### Migration 0008: concurrency and exposure assessment

* The migration runs as the **table owner**. `FORCE ROW LEVEL SECURITY` is lifted on `document_versions` and `audit_events` only inside the migration transaction and restored before commit; `ALTER TABLE ... FORCE` is transactional, so any failure restores it by rolling back (rehearsed with an injected mid-backfill error: `FORCE` stayed on for all 12 tables, owner and application role saw 0 rows without tenant context).
* The application role is not the owner, so RLS always applied to it. While the transaction is open the two tables are held `ACCESS EXCLUSIVE`, so no application statement can run against them at all; no request can observe rows without RLS. Other sessions wait, they do not see data.
* **Write quiescence is required** because readers and writers on those two tables block for the duration (0.4 s on production-sized data) and a pending lock queues other traffic. `lock_timeout` makes the migration abort after 30 s instead of blocking indefinitely (rehearsed). Transactional DDL does not make concurrent-operation scenarios safe on its own; the runbook pauses writes and stops the application first.
* Backfill correctness was checked against an independent Python oracle on 6,711 real versions (0 mismatches) and on seeded branching, unknown-legacy and cross-tenant histories (16 checks).

## 3. Fixes introduced during this review

R1, R2 and R3 above, plus: `scripts/dev/isolated-stack.sh` gained `C360_ISO_NO_MIGRATE=1` (adopt a restored isolated copy as-is), new test and verification scripts (`lineage_oracle.py`, `rehearsal_walkthrough.py`), and tests for the legacy render entry point, migration lock timeout and the stack script.

## 4. Regression results (fresh isolated PostgreSQL, production-build frontend)

| Suite | Result |
|---|---|
| Backend `pytest tests` | **751 passed**, 0 failed, 0 skipped |
| Playwright, all specs except ZIP | **88 passed**, 0 failed (also 88 passed against the restored, migrated copy) |
| Non-ZIP stakeholder acceptance | **17 PASS**, 0 FAIL; ZIP stage reported OUT OF SCOPE |
| `compare_flow` / `branch_compare_flow` | 31/31, 11/11 |
| `xlsx_fidelity` / `xlsx_index_fidelity` | 91/91, 36/36 |
| `docx_fidelity` / `docx_geometry` | 26/26, 9/9 |
| `signed_flow` / `security_flow` | 82/82, 23/23 (also on the restored copy) |
| `xlsx_benchmark` / `xlsx_compare_stress` | 32/32, 20/20 |
| Seatbelt live confinement (`test_sandbox.py`) | 16/16 |
| Migration 0008 on PostgreSQL (`lineage_migration_check.py`) | seed, migrate, SQL 16/16, churn, API 14/14, rollback 4/4 |

ZIP archive import is out of scope: not restored, not tested; behaviour is unchanged.

## 5. Fresh pre-deployment backup

A timestamped directory under `~/channel360-backups/` (mode 700, contents mode 600; nothing existing was overwritten or modified). Contents: custom-format `pg_dump` of `channel360_test`, rsync copy of `.runtime/document-storage`, code snapshots (the exported running baseline `1df628a`, a git bundle of both branches, target SHA), redacted runtime configuration, separately stored secrets (role dump, pgpass, development auth secret), `BACKUP_MANIFEST.md` and a SHA-256 manifest of every file. Source facts at backup time: PostgreSQL 16.15, Alembic `0007_edit_set_supersession`, 174 tenants, 4,440 documents, 6,711 versions, 6,711 file rows, 2,840 edit sets, 42,283 audit events, 4,801 storage files (527 MB), 337 GiB free.

## 6. Backup integrity

* `pg_dump` exit 0, no errors, 136 TOC entries, 1.0 s. Storage copy 4.0 s; 4,801 source files and 4,801 copied files, identical size.
* SHA-256 manifest of 4,816 files re-verified with 0 failures. Every present object referenced by the dump matches its stored size and SHA-256 (4,794 of 4,794, 0 mismatches).
* **Pre-existing data condition:** 1,917 of 6,711 file rows (1,914 created 2026-11-13, keys of the `test/key` kind) reference objects that already do not exist in the live storage: leftovers of earlier test runs against this database. The backup mirrors live; it cannot contain what live lacks. Reads of those versions return a controlled 409 (`stored_file_unavailable`).
* **Consistency limitation:** the shared backend was not stopped. The database snapshot is internally consistent (one transaction). The storage copy was taken after the dump; storage only grows, so every referenced object that exists live is in the copy, and the copy may hold extra objects. This is **not** an atomic database-plus-storage snapshot. The production cut-over must take a final backup with writes paused.

## 7. Restore into isolated resources

Restored into a new database and a new storage directory (neither connected to the shared ones). Restore took 0.8 s (database) and 4.6 s (storage). Verified: counts equal the shared source for all six tables (174 / 4,440 / 6,711 / 6,711 / 42,283 / 2,840); the ordered hash of version ids and SHA-256s and the audit hash chain digest are identical to the source; 0 versions without a file, 0 missing parents, 0 missing current versions, 0 cross-tenant file references; 4,794 present objects match their hashes; 12 tables carry `FORCE ROW LEVEL SECURITY`; the owner role sees 0 rows without tenant context.

## 8. Migration 0008 rehearsal on the restored real data

Starting state: revision `0007`, table owner `channel360_test_migrator`, 12 tables with `FORCE` on, 4,440 original and 2,271 derived versions (2,017 with an edit set, 2,271 with release events, 0 working-copy events). Executed `alembic upgrade head` as the owner: **0.39 s**, revision `0008_version_lineage_class`. Result: 4,440 `original`, 2,271 `standard`, matching an independent oracle exactly (0 mismatches, 0 extra rows). Column `text NOT NULL`, CHECK constraint present, `FORCE` restored on all 12 tables. Audit chain digest and version hash unchanged. As the (non-owner) application role each of the three largest tenants saw exactly its own rows and only its own classes; with no tenant context it saw 0.

## 9. Migration failure rehearsal (separate disposable restored copies)

| Scenario | Outcome |
|---|---|
| Concurrent writer holds a lock on `audit_events` | Migration aborted after 31 s (`LockNotAvailable`); revision still `0007`, no column, `FORCE` on all 12 tables; retry in a quiet window succeeded |
| Error injected inside the backfill `UPDATE` (after `FORCE` was lifted and the column added) | Full rollback: no column, no constraint, revision `0007`, `FORCE` on all 12 tables, app role and owner both see 0 rows without tenant context; rerun after removing the fault succeeded |

No misleading success state was recorded in either case.

## 10. Deployment rehearsal and four-feature walkthrough

The new code was started against the migrated restored copy (macOS, same LibreOffice, OpenSSL and sandbox as intended for the shared app): sandbox self-test verified, health OK, restored tenant's 141 documents listed. On **historical** data (12 sampled multi-version PDF/XLSX/DOCX documents of the restored tenant): history labels only `original`/`standard` (the real data contains no working copies or unknown history), comparison completed 12/12, downloads honoured eligibility (18 allowed, 3 refused), another tenant received 404 for history and download. The full browser suite then passed 88/88 against this stack, covering: (A) multi-version comparison with highlights and branching, (B) Excel expand, navigate, edit, save and independent reopen, (C) Word fidelity, edit, save and reopen, (D) signed Motor policy: truthful signature status, acknowledged unsigned working copy, edit, download, original hashes unchanged. The corpus flows (signed 82/82, security 23/23, DOCX fidelity 26/26, comparison 31/31) also passed there. All test changes stayed in the restored copy.

## 11. Rollback rehearsal

| Path | What was done | Result |
|---|---|---|
| B. Restore from backup | New database + storage from the backup (5.4 s), previous revision `1df628a` (extracted from the backup) started on :8011 | Starts; no comparison routes (old API); 141 documents / 192 versions; downloads work; other tenant 404; version hash equals the backup snapshot |
| A. Schema downgrade of the upgraded copy | `alembic downgrade 0007` (0.29 s) on a copy that held data written by the new app | `FORCE` on all 12 tables; 6,882 versions unchanged (hash identical); old revision serves it (141 documents) |

Caveats: the downgrade drops `lineage_class`, so working-copy classifications are not retained (the old revision derives them from audit events); a schema downgrade is **not** a data recovery plan; anything written after the backup is lost on a full restore.

## 12. Security checks

* Outgoing changes contain no credentials, keys, dumps, logs or stakeholder content; `Policy Copies/`, `.private/` and `.runtime/` are ignored and untracked (only stakeholder fixture file names appear in test scripts).
* Backup directory mode 700, all files 600, secrets separate from shareable text.
* Fail-closed demonstrated: with the confinement facility missing, `confined_runner`, the preview renderer and the visual-verification renderer all refuse; with confinement replaced by a no-op (leaky), the self-test detects it (`verified_here: false`) and every runner refuses.
* Residual risks: Linux Bubblewrap **NOT RUNTIME VERIFIED**; container confinement **NOT VERIFIED**; LibreOffice still needs `mach-lookup` and an exact-pattern IPC pipe under `/private/var/tmp`; isolated databases share role passwords with the shared one (R7); ZIP import not in scope.

## 13. Performance observations

| Workbook | Cells | Index ready | Max window | Native save | Complete comparison |
|---|---|---|---|---|---|
| small RFQ | 604 | 0.23 s | 6 ms | 0.6 s | 0.24 s |
| medium QCR | 312 | 0.23 s | 6 ms | 0.7 s | 0.23 s |
| synthetic | 60,830 | 1.7 s | 6 ms | 4.0 s | 1.8 s |
| synthetic | 180,830 | 4.8 s | 6 ms | 11.3 s | 5.1 s |

180,830 cells with 174,150 changes: scan complete, list truncated and flagged (98,222 returned), peak 162 MB (+85 MB). Operations on production-sized data: backup 5 s, restore 5.4 s, migration 0.39 s, downgrade 0.29 s.

## 14. Required dependencies

macOS (verified on Darwin 25.6, Apple silicon), PostgreSQL 16, LibreOffice with Carlito/Liberation/Caladea fonts, OpenSSL 3 or later, host SQLite library, ClamAV socket, the project virtual environment. No new environment variables. `.preview-cache` and `.xlsx-index` are derived and disposable.

## 15. Production deployment runbook (macOS; do not run without explicit approval)

**Pre-deployment**
1. Written approval from the owner for: merging PR #2, the maintenance window, the migration. Record current revision (`1df628a` backend, `0007`) and target (the merged `main` SHA).
2. Check out the target into a **dedicated release directory** (not the development worktree); build the frontend (`npm run build`); keep the previous directory for rollback.
3. Confirm the migration role owns `document_versions` and `audit_events`; `alembic current` is `0007_edit_set_supersession`; free disk at least 2x the backup size; LibreOffice, OpenSSL and ClamAV present; `GET /health/sandbox` on a staging start reports `verified_here: true`.
4. Announce a maintenance window (expected total about 10 minutes of write downtime, measured steps below add up to under 1 minute plus operator time).

**Deployment**
5. Stop accepting writes: stop the frontend server and the backend (stop order: frontend, then backend; confirm no process on `:8000`/`:5174` and `pg_stat_activity` shows no application sessions).
6. **Final consistent backup with writes paused**: `pg_dump -Fc` of `channel360_test`, then rsync of `.runtime/document-storage`, then SHA-256 manifest and verification (about 5 s each at current size). Do not overwrite earlier backups.
7. Export migration environment; run `alembic upgrade head` as the table owner (about 0.4 s). Expect `0008_version_lineage_class`. If it fails with a lock timeout, find the blocking session, retry; nothing is half-applied.
8. Verify: revision `0008`; `relforcerowsecurity` true on all 12 tables; `lineage_class` NOT NULL with CHECK; class counts equal the oracle (`scripts/dev/lineage_oracle.py compare`).
9. Start the new backend from the release directory with the preserved environment; start the new frontend (production build served statically, not a dev server). Check `/health/sandbox` and the OpenAPI revision.

**Post-deployment**
10. Verify revision, document and version counts against the backup, storage SHA-256 sample, authentication, tenant isolation (a second tenant gets 404 on another tenant's document), history labels, comparison, an XLSX open/edit/save, a DOCX render/edit/save, a signed-PDF working copy with the "not official" marking, download restrictions, worker and API logs free of sandbox errors.
11. Keep the maintenance banner until all checks pass; then resume normal traffic.

**Rollback** (triggers: migration failure that cannot be retried, sandbox not verified, data or isolation check fails, error rate after start)
12. Stop the new frontend and backend. Fast path: `alembic downgrade 0007_edit_set_supersession` (0.3 s) and start the previous revision from the preserved export of `1df628a` using the saved run script. Full path (if data is in doubt): restore the database from the final backup into `channel360_test` with `pg_restore --clean` (about 1 s) and restore storage from the matching copy (about 5 s) **only with separate approval**, then start the previous revision.
13. Verify counts, hashes, history and tenant isolation as in step 10. Data written after step 6 is lost on a full restore; the downgrade keeps it but drops `lineage_class`.

## 16. Remaining limitations and approvals still required

* Linux Bubblewrap not runtime verified; container confinement not verified; ZIP out of scope; LibreOffice `mach-lookup`/IPC-pipe grants; shared role passwords across isolated and shared databases; 1,917 pre-existing file rows without objects in the shared storage (test leftovers; harmless to the migration, reads return 409).
* The shared frontend dev server currently serves source from the development worktree (R6); decide before deployment whether to stop using that arrangement.
* Required approvals not yet given: merge PR #2; deploy to the shared environment; run migration 0008 on `channel360_test`; restart the shared backend and frontend; any full restore.

## 17. Estimated maintenance requirement

From measured rehearsal steps: stop services (operator), backup 5 s (database 1 s, storage 4 s) plus verification, migration 0.4 s, start and health checks (operator), post-checks 5 to 10 minutes of manual verification. Plan a **15 to 30 minute** write-paused window; rollback by downgrade is under 1 minute of execution, a full restore under 10 seconds of execution plus verification.

## 18. Final recommendation

**READY FOR CONTROLLED MACOS DEPLOYMENT**: the regression suites pass on fresh and restored data, the backup is verified, the migration, its two failure modes, the deployment and both rollback paths were rehearsed on isolated copies of the real data, and no unresolved critical or high finding remains. Conditions: the approvals in section 16, a write-paused window with a final consistent backup, deployment from a dedicated release directory, and no use for Linux or containers.
