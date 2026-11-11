import { useCallback, useEffect, useRef, useState } from "react";
import { Icon } from "./Shell";
import {
  ApiError, fetchVersionFile, folderName, getPreview, saveVersion, typeLabel,
  type Chunks, type FolderVersion, type Preview, type SaveBody,
} from "../lib/api";
import { DocxEditor, GridEditor, PdfEditor, TextEditor } from "./editors";

export type EditsProps = {
  documentId: string; folderName: string; version: FolderVersion; family: FolderVersion[]; chunksText: Chunks | null;
  setDirty: (dirty: boolean) => void; notify: (message: string, error?: boolean) => void;
  refresh: () => Promise<void>; onSaved: (versionId: string) => void; download: () => void;
};

const DESCRIPTION: Record<string, string> = {
  text: "Edit text below. Your original is preserved.",
  grid: "Click a cell to change its value. Locked cells can't be edited. Your original is preserved.",
  docx: "Edit the highlighted text directly. Dimmed text is read-only. Your original is preserved.",
  pdf: "Fill in the form fields on the page. Your original is preserved.",
};

export function EditsTab(props: EditsProps) {
  const { documentId, version, notify } = props;
  const [preview, setPreview] = useState<Preview | null>(null);
  const [body, setBody] = useState<SaveBody | null>(null);
  const [saving, setSaving] = useState(false);
  const [resetKey, setResetKey] = useState(0);
  const dirty = body !== null;
  const bodyRef = useRef<SaveBody | null>(null);

  useEffect(() => {
    let live = true;
    getPreview(documentId, version.version_id).then((p) => { if (live) setPreview(p); }).catch(() => { if (live) notify("The editor could not be opened.", true); });
    return () => { live = false; };
  }, [documentId, version.version_id, notify]);

  const onBody = useCallback((next: SaveBody | null) => {
    bodyRef.current = next; setBody(next); props.setDirty(next !== null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => () => props.setDirty(false),
  // eslint-disable-next-line react-hooks/exhaustive-deps
  []);

  async function save() {
    const current = bodyRef.current;
    if (!current || saving) return;
    setSaving(true);
    try {
      const result = await saveVersion(documentId, version.version_id, current);
      await props.refresh();
      const number = result.version?.number ?? props.family.length + 1;
      props.notify(`Version v${number} saved inside ${folderName(props.folderName)}. Original preserved.${result.note ? ` ${result.note}` : ""}`);
      props.setDirty(false);
      props.onSaved(result.version_id);
    } catch (error) {
      notify(error instanceof ApiError ? error.message : "This change couldn't be saved safely. Your edits are still here.", true);
    } finally { setSaving(false); }
  }

  function reset() {
    if (!window.confirm("Discard changes in this working copy?")) return;
    onBody(null); setResetKey((k) => k + 1);
  }

  const head = (
    <>
      <div className="section-eyebrow">03 / The transformation</div>
      <div className="section-title"><h3>Edit this version.</h3><span className="pill orange">New folder version</span></div>
    </>
  );
  if (!preview) return <>{head}<p className="stage-note">Opening the editor…</p></>;
  const mode = preview.edit.mode;
  if (mode === "none") {
    return (
      <>
        {head}
        <div className="empty"><h3>No editable text available</h3><p>{preview.edit.reason}</p></div>
      </>
    );
  }
  const kind = preview.kind ?? "";
  const formatting = mode === "text" && (kind === "md" || kind === "html");
  return (
    <>
      {head}
      <div className="edit-head">
        <div><strong><Icon name="edit" size={15} /> Working document</strong><div><span>{DESCRIPTION[mode]}</span></div></div>
        <span id="edit-state">{dirty ? "Unsaved changes" : "No changes yet"}</span>
      </div>
      {mode === "text" && <TextEditor key={resetKey} preview={preview} formatting={formatting} onBody={onBody} onReset={reset} />}
      {mode === "grid" && <GridEditor key={resetKey} preview={preview} onBody={onBody} />}
      {mode === "docx" && <DocxEditor key={resetKey} preview={preview} onBody={onBody} />}
      {mode === "pdf" && <PdfEditor key={resetKey} preview={preview} fetchBytes={async () => (await fetchVersionFile(documentId, version.version_id)).arrayBuffer()} onBody={onBody} />}
      <div className="edit-actions">
        <div>
          <button className="btn dark" disabled={!dirty || saving} onClick={save}><Icon name="check" size={15} /> Save as next version</button>
          <button className="btn outline" onClick={props.download}><Icon name="download" size={15} /> Export {typeLabel(preview.name)}</button>
        </div>
        <span className="stage-note">{dirty ? "Modified working copy" : "Your changes stay separate from the source"}</span>
      </div>
      <details className="compare-snapshot">
        <summary>Compare against source text</summary>
        <pre>{props.chunksText?.excerpt ?? ""}{props.chunksText?.excerpt_truncated ? "\n… excerpt shortened" : ""}</pre>
      </details>
      <p className="download-note" style={{ marginTop: 18 }}>Saving creates the next version in the same format as the original. The original file is never changed.</p>
    </>
  );
}
