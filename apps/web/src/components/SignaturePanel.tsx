import { useState } from "react";
import { ApiError, createWorkingCopy, type SignatureInfo } from "../lib/api";

const TONE: Record<string, string> = {
  none: "ok", unsigned_signature_field: "ok", integrity_valid_trusted: "ok",
  integrity_valid_trust_not_established: "warn", integrity_valid_untrusted: "warn", modified_after_signing: "warn", not_validated: "warn",
  malformed: "bad", integrity_invalid: "bad",
};
const WORD: Record<string, string> = { valid: "Integrity valid", invalid: "Integrity broken", malformed: "Malformed", unverifiable: "Could not verify" };
const TRUST: Record<string, string> = { not_established: "Trust not established", trusted_by_policy: "Trusted by configured policy", not_trusted: "Not trusted" };

/** Truthful signature status: integrity, certificate trust and coverage are shown separately; legal acceptance is never claimed. */
export function SignatureStatus({ signature }: { signature: SignatureInfo }) {
  const state = signature.state ?? signature.status;
  if (state === "none") return null;
  return (
    <div className={`signature-panel tone-${TONE[state] ?? "warn"}`} data-testid="signature-status" data-state={state}>
      <div className="sig-head"><b>{signature.label ?? state}</b><span className="pill">{signature.validated ? "Trusted by policy" : "Not validated as trusted"}</span></div>
      <p>{signature.detail}</p>
      {signature.signatures?.length ? (
        <ul className="sig-list">
          {signature.signatures.map((s, i) => (
            <li key={i}>
              <span>{WORD[s.integrity ?? "unverifiable"] ?? s.integrity}</span>
              {s.trust ? <span>{TRUST[s.trust] ?? s.trust}</span> : null}
              <span>{s.covers_whole_file === false ? "Does not cover the whole file" : "Covers the whole file"}</span>
              {s.signer?.issuer ? <span title={s.signer.subject}>Issued by {(/CN=([^,]+)/.exec(s.signer.issuer)?.[1] ?? /O=([^,]+)/.exec(s.signer.issuer)?.[1] ?? "unknown authority")}</span> : null}
              {s.reason ? <span className="sig-reason">{s.reason.replace(/_/g, " ")}</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
      {signature.docmdp ? <p className="stage-note">Document permissions set by the signer: {signature.docmdp.meaning}.</p> : null}
      <p className="stage-note">Cryptographic integrity, certificate trust and legal acceptance are different things. This check never claims legal acceptance.</p>
    </div>
  );
}

export function WorkingCopyAction({ documentId, versionId, acknowledgement, onCreated, notify }: {
  documentId: string; versionId: string; acknowledgement: string; onCreated: (versionId: string, note: string) => void; notify: (m: string, error?: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const [agreed, setAgreed] = useState(false);
  const [busy, setBusy] = useState(false);
  async function confirm() {
    setBusy(true);
    try { const r = await createWorkingCopy(documentId, versionId); setOpen(false); onCreated(r.version_id, r.note); }
    catch (e) { notify(e instanceof ApiError ? e.message : "The working copy could not be created.", true); }
    finally { setBusy(false); }
  }
  return (
    <>
      <button className="btn dark" data-testid="create-working-copy" onClick={() => { setAgreed(false); setOpen(true); }}>Create editable working copy</button>
      {open ? (
        <div className="overlay" role="dialog" aria-modal="true" aria-labelledby="wc-title">
          <div className="process-modal wc-modal">
            <div className="kicker">Unsigned working copy</div>
            <h2 id="wc-title">Create an editable copy?</h2>
            <p>{acknowledgement}</p>
            <label className="wc-ack"><input type="checkbox" checked={agreed} onChange={(e) => setAgreed(e.target.checked)} /> I understand the working copy is not signed or approved by the original signer.</label>
            <div className="wc-actions">
              <button className="btn outline" onClick={() => setOpen(false)} disabled={busy}>Cancel</button>
              <button className="btn dark" disabled={!agreed || busy} onClick={confirm} data-testid="confirm-working-copy">{busy ? "Creating…" : "Create working copy"}</button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}

export function WorkingCopyBanner() {
  return (
    <div className="wc-banner" role="note" data-testid="working-copy-banner">
      <b>DRAFT — MODIFIED COPY — NOT OFFICIAL</b>
      <span>This is an unsigned working copy. It does not carry the original signer's signature and was not issued or approved by them. The signed original is kept unchanged.</span>
    </div>
  );
}
