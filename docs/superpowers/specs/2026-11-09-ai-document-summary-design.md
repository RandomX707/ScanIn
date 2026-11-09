# AI Document Summary — Short Design

**Intent:** Add an explicit, optional AI explanation alongside the deterministic summary so operations users can compare structure with a plain-English, source-grounded interpretation.

**Goals:** Preserve No AI as the default and fully functional offline; call an OpenAI-compatible HTTPS API only after a click; bind every response to the authorized immutable document version; keep the API key server-side; provide an honest compare view.

**Non-goals:** No model hosting, OCR, automated decisions, edits, classification, approval, verification or release changes.

**Approach:** Use the existing tenant-scoped view authorization and restricted parser. A dedicated endpoint re-resolves and hashes the stored source, asks the restricted parser for a small bounded evidence bundle, and calls a backend provider adapter. The adapter accepts only configured HTTPS endpoints, disables redirects and ambient proxy configuration, and validates a structured response against server-issued evidence references. React stores responses only in current-session memory keyed by document and version.

**Constraints:** No browser-supplied text or hash is trusted; no external request occurs on inspection or mode switching; untrusted document text cannot change system instructions; only synthetic documents are used for any live smoke test; all AI output is informational and must not affect authorization or workflow state.

**Alternatives considered:** (1) client-side provider calls were rejected because they expose credentials and source content; (2) always-on/background summarization was rejected because it sends data without a deliberate user action; (3) server-side explicit request was selected because it preserves existing authorization and parser boundaries.

**Implementation calls:** Keep response claims as evidence-linked items and build the displayed explanation only from claims whose references resolve to server-issued evidence. If evidence is absent or invalid, return a safe provider-response error. Keep an unconfigured provider usable as a clear unavailable state while No AI remains unaffected.
