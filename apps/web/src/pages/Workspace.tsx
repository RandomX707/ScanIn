import type { Folder } from "../lib/api";

export function Workspace(_props: {
  documentId: string; versionId: string; folders: Folder[]; onHome: () => void; onHistory: () => void;
  onFolder: (id: string) => void; onOpen: (d: string, v: string) => void; onBrowse: () => void;
  notify: (m: string, e?: boolean) => void; refresh: () => Promise<void>;
}) {
  return <main className="main workspace-shell"><p>Workspace</p></main>;
}
