import { useEffect, useState } from "react";
import { getPreview, type Chunks, type FolderVersion, type Preview } from "../lib/api";

export type EditsProps = {
  documentId: string; version: FolderVersion; family: FolderVersion[]; chunksText: Chunks | null;
  setDirty: (dirty: boolean) => void; notify: (message: string, error?: boolean) => void;
  refresh: () => Promise<void>; onSaved: (versionId: string) => void; download: () => void;
};

export function EditsTab({ documentId, version }: EditsProps) {
  const [preview, setPreview] = useState<Preview | null>(null);
  useEffect(() => { void getPreview(documentId, version.version_id).then(setPreview).catch(() => setPreview(null)); }, [documentId, version.version_id]);
  return (
    <>
      <div className="section-eyebrow">03 / The transformation</div>
      <div className="section-title"><h3>Edit this version.</h3><span className="pill orange">New folder version</span></div>
      <div className="empty"><h3>No editable text available</h3><p>{preview?.edit.reason ?? "Loading…"}</p></div>
    </>
  );
}
