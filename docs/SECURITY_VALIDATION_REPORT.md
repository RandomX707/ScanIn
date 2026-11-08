# Security Validation Report

Validation date: 2026-11-08. Scope: local development prototype and the dedicated `channel360_test` database. This is not a production security certification.

## Executed controls

- PostgreSQL integration suite: 22 passed. The tests exercise tenant RLS, cross-tenant isolation, role boundaries, reviewer separation, immutable/frozen edit sets, release transaction behavior, audit chain, and verified-only download.
- ClamAV suite: 4/4 passed against ClamAV 1.5.4: clean synthetic file accepted; EICAR quarantined; executable rejected; unavailable scanner remains private and unpromoted.
- Browser workflows: 6/6 passed using separate server-issued processor, administrator and reviewer identities. Browser checks include cross-tenant denial, rejection, approval, verification, original hash preservation and formula-dependent XLSX download denial.
- Local-demo session issuance is server-side, short-lived, loopback-only and restricted to explicitly configured local development. The browser contains no signing secret.
- The application launcher uses the existing private PGPASSFILE, validates restrictive permissions, targets `channel360_test`, verifies the expected owner and refuses missing PostgreSQL or ClamAV. No database or role reset was performed.
- Generated browser traces/results, runtime state, storage, credentials and derived downloads are excluded from Git. Only synthetic M0 source workbooks and credential-free UI screenshots are retained.

## Remaining security limitations

- No production identity provider, enterprise key management, MFA, deployment hardening, retention review or production incident procedures.
- Parser OS-level network isolation has not been independently validated on every target host.
- The filesystem storage adapter and synchronous processing path are development implementations; durable queue, retries, monitoring and event delivery are absent.
- Local PostgreSQL/ClamAV passing tests do not establish production service configuration or availability.

## Credential handling incident during validation

An initial failed browser-runner invocation printed one short-lived synthetic test bearer token in its network error output. The local development signing key was rotated immediately before subsequent runs, invalidating tokens signed with the prior key. The failure trace and test results are ignored by Git and are not included in the public staging set. No database password, signing key or production credential was printed or added to Git.

The active local app uses the rotated development key. See the [README](../README.md) and [known limitations](KNOWN_LIMITATIONS.md) for product scope.
