import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, getPdfText, previewPdfEdits, type PdfPageText, type PdfRun, type PdfTextEdit, type Preview, type SaveBody } from "../lib/api";

type PdfLib = typeof import("pdfjs-dist");
type Doc = import("pdfjs-dist").PDFDocumentProxy;
type Props = {
  documentId: string; versionId: string; preview: Preview; fetchBytes: () => Promise<ArrayBuffer>;
  onBody: (body: SaveBody | null) => void; notify: (message: string, error?: boolean) => void;
};

/** Ordinary (non-form) PDF: click existing text on the page, change it, and see the real edited PDF re-rendered. */
export function PdfTextEditor({ documentId, versionId, preview, fetchBytes, onBody, notify }: Props) {
  const pages = preview.pages ?? [];
  const [lib, setLib] = useState<PdfLib | null>(null);
  const [doc, setDoc] = useState<Doc | null>(null);
  const [edits, setEdits] = useState<Record<string, PdfTextEdit>>({});
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const original = useRef<ArrayBuffer | null>(null);
  const seq = useRef(0);

  const open = useCallback(async (bytes: ArrayBuffer, pdfjs: PdfLib) => pdfjs.getDocument({ data: new Uint8Array(bytes.slice(0)) }).promise, []);

  useEffect(() => {
    let live = true;
    (async () => {
      const pdfjs = await import("pdfjs-dist");
      pdfjs.GlobalWorkerOptions.workerSrc = (await import("pdfjs-dist/build/pdf.worker.min.mjs?url")).default;
      original.current = await fetchBytes();
      const opened = await open(original.current, pdfjs);
      if (live) { setLib(pdfjs); setDoc(opened); }
    })().catch(() => notify("The document could not be opened.", true));
    return () => { live = false; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Apply the whole edit set to the original on the server and show exactly what would be saved. */
  async function apply(next: Record<string, PdfTextEdit>, runId: string): Promise<boolean> {
    const list = Object.values(next);
    const mine = ++seq.current;
    setBusy(true);
    try {
      if (!list.length) {
        if (lib && original.current) setDoc(await open(original.current, lib));
      } else {
        const bytes = await previewPdfEdits(documentId, versionId, list);
        if (mine !== seq.current) return true;
        if (lib) setDoc(await open(bytes, lib));
      }
      setEdits(next); setErrors((e) => { const { [runId]: _drop, ...rest } = e; return rest; });
      onBody(list.length ? { pdf_edits: list } : null);
      return true;
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "That change couldn't be applied safely.";
      setErrors((e) => ({ ...e, [runId]: message }));
      notify(message, true);
      return false;
    } finally { if (mine === seq.current) setBusy(false); }
  }

  const change = useCallback(async (run: PdfRun, value: string) => {
    const current = edits[run.id]?.new ?? run.text;
    if (value === current) return;
    const next = { ...edits };
    if (value === run.text) delete next[run.id]; else next[run.id] = { id: run.id, old: run.text, new: value };
    await apply(next, run.id);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [edits, lib]);

  return (
    <div className="pdf-editor" role="region" aria-label="PDF text editor" aria-busy={busy}>
      {doc && lib ? pages.map((page) => (
        <TextPage key={page.page} doc={doc} documentId={documentId} versionId={versionId} page={page}
          edits={edits} errors={errors} onChange={change} />
      )) : <p className="stage-note">Opening the document…</p>}
    </div>
  );
}

function TextPage({ doc, documentId, versionId, page, edits, errors, onChange }: {
  doc: Doc; documentId: string; versionId: string; page: { page: number; width: number; height: number };
  edits: Record<string, PdfTextEdit>; errors: Record<string, string>; onChange: (run: PdfRun, value: string) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [visible, setVisible] = useState(page.page <= 2);
  const [text, setText] = useState<PdfPageText | null>(null);
  const [active, setActive] = useState<string | null>(null);

  useEffect(() => {
    const el = host.current; if (!el || visible) return;
    const watcher = new IntersectionObserver((entries) => { if (entries.some((e) => e.isIntersecting)) setVisible(true); }, { rootMargin: "600px" });
    watcher.observe(el);
    return () => watcher.disconnect();
  }, [visible]);

  useEffect(() => {
    if (!visible || text) return;
    let live = true;
    getPdfText(documentId, versionId, page.page).then((t) => { if (live) setText(t); }).catch(() => undefined);
    return () => { live = false; };
  }, [visible, text, documentId, versionId, page.page]);

  useEffect(() => {
    if (!visible) return;
    let cancelled = false;
    (async () => {
      const p = await doc.getPage(page.page);
      const viewport = p.getViewport({ scale: 1.6 });
      const el = canvas.current; if (!el || cancelled) return;
      el.width = viewport.width; el.height = viewport.height;
      const context = el.getContext("2d"); if (!context) return;
      await p.render({ canvasContext: context, viewport }).promise;
    })().catch(() => undefined);
    return () => { cancelled = true; };
  }, [doc, visible, page.page]);

  const runs = useMemo(() => text?.runs ?? [], [text]);
  return (
    <div ref={host} className="pdf-page" style={{ aspectRatio: `${page.width} / ${page.height}` }}>
      <canvas ref={canvas} aria-label={`Page ${page.page}`} />
      {runs.map((run) => {
        const [x0, y0, x1, y1] = run.bbox;
        const style: React.CSSProperties = {
          left: `${(x0 / page.width) * 100}%`, top: `${((page.height - y1) / page.height) * 100}%`,
          width: `${((x1 - x0) / page.width) * 100}%`, height: `${((y1 - y0) / page.height) * 100}%`,
          fontSize: `${Math.max(1.4, (run.size / page.width) * 100)}cqw`,
        };
        const edited = run.id in edits;
        const value = edits[run.id]?.new ?? run.text;
        if (active === run.id) {
          return (
            <input key={run.id} className="pdf-field pdf-run-input" style={{ ...style, minWidth: "8%" }} autoFocus defaultValue={value}
              aria-label={`Edit text: ${run.text}`} data-run={run.id}
              onBlur={(e) => { setActive(null); onChange(run, e.currentTarget.value); }}
              onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); if (e.key === "Escape") { e.currentTarget.value = value; e.currentTarget.blur(); } }} />
          );
        }
        return (
          <button key={run.id} type="button" style={style} data-run={run.id} data-locked={run.e ? undefined : "true"}
            className={`pdf-run${edited ? " changed" : ""}${errors[run.id] ? " refused" : ""}`}
            title={errors[run.id] ?? (run.e ? "Click to edit this text" : run.r ?? "This text is read-only.")}
            aria-label={`${run.e ? "Edit" : "Read-only"} text: ${value}`} aria-disabled={!run.e}
            onClick={() => { if (run.e) setActive(run.id); }} />
        );
      })}
    </div>
  );
}
