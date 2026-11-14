import { useCallback, useEffect, useRef, useState } from "react";

type Doc = import("pdfjs-dist").PDFDocumentProxy;

export type Highlight = { id: string; page: number; bbox: [number, number, number, number]; kind: "add" | "del" | "mod"; label?: string };
export type PageViewerProps = {
  /** Resolves the PDF bytes to display (a native PDF, or a preview-only rendering of an Office document). */
  fetchBytes: () => Promise<ArrayBuffer>;
  title: string;
  highlights?: Highlight[];
  activeHighlight?: string | null;
  initialPage?: number;
  note?: string;
  onPages?: (pages: { page: number; width: number; height: number }[]) => void;
  testId?: string;
  /** Show only these pages (comparison 'changed content only'); page numbers keep their real values. */
  onlyPages?: number[] | null;
  heading?: string;
};

const ZOOMS = [0.5, 0.75, 1, 1.25, 1.5, 2, 3];

/** Shared paginated viewer: zoom, fit width, actual size, page navigation, full-screen. Used by Original and Compare. */
export function PageViewer({ fetchBytes, title, highlights = [], activeHighlight, initialPage, note, onPages, testId, onlyPages, heading }: PageViewerProps) {
  const [doc, setDoc] = useState<Doc | null>(null);
  const [pages, setPages] = useState<{ page: number; width: number; height: number }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [zoom, setZoom] = useState<number | "fit">("fit");
  const [expanded, setExpanded] = useState(false);
  const [current, setCurrent] = useState(initialPage ?? 1);
  const scroller = useRef<HTMLDivElement>(null);
  const [boxWidth, setBoxWidth] = useState(760);

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        pdfjs.GlobalWorkerOptions.workerSrc = (await import("pdfjs-dist/build/pdf.worker.min.mjs?url")).default;
        const bytes = await fetchBytes();
        const opened = await pdfjs.getDocument({ data: new Uint8Array(bytes) }).promise;
        const sizes: { page: number; width: number; height: number }[] = [];
        for (let i = 1; i <= opened.numPages; i++) { const v = (await opened.getPage(i)).getViewport({ scale: 1 }); sizes.push({ page: i, width: v.width, height: v.height }); }
        if (live) { setDoc(opened); setPages(sizes); onPages?.(sizes); }
      } catch { if (live) setError("The page preview could not be opened."); }
    })();
    return () => { live = false; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const el = scroller.current; if (!el) return;
    const measure = () => setBoxWidth(Math.max(280, el.clientWidth - 36));
    measure();
    const ro = new ResizeObserver(measure); ro.observe(el);
    return () => ro.disconnect();
  }, [expanded, doc]);

  useEffect(() => {
    if (!expanded) return;
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") setExpanded(false); };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [expanded]);

  const maxWidth = pages.reduce((m, p) => Math.max(m, p.width), 0) || 595;
  const scale = zoom === "fit" ? boxWidth / maxWidth : (zoom * 96) / 72;    // 100% = 96dpi "actual size"
  const goto = useCallback((n: number) => {
    const target = Math.min(Math.max(1, n), Math.max(1, pages.length));
    setCurrent(target);
    scroller.current?.querySelector(`[data-page="${target}"]`)?.scrollIntoView({ block: "start" });
  }, [pages.length]);

  useEffect(() => { if (initialPage && pages.length) goto(initialPage); }, [initialPage, pages.length, goto]);
  useEffect(() => {
    if (!activeHighlight) return;
    const h = highlights.find((x) => x.id === activeHighlight);
    if (h) { setCurrent(h.page); requestAnimationFrame(() => scroller.current?.querySelector(`[data-hl="${CSS.escape(activeHighlight)}"]`)?.scrollIntoView({ block: "center" })); }
  }, [activeHighlight, highlights]);

  const stepZoom = (dir: 1 | -1) => {
    const now = zoom === "fit" ? scale * 72 / 96 : zoom;
    const next = dir > 0 ? ZOOMS.find((z) => z > now + 0.01) : [...ZOOMS].reverse().find((z) => z < now - 0.01);
    if (next) setZoom(next);
  };
  const track = useRef<IntersectionObserver | null>(null);
  useEffect(() => {
    const root = scroller.current; if (!root || !pages.length) return;
    track.current?.disconnect();
    const io = new IntersectionObserver((entries) => {
      const top = entries.filter((e) => e.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (top) setCurrent(Number((top.target as HTMLElement).dataset.page));
    }, { root, threshold: [0.25, 0.6] });
    root.querySelectorAll("[data-page]").forEach((n) => io.observe(n));
    track.current = io;
    return () => io.disconnect();
  }, [pages.length, scale]);

  const percent = Math.round(scale * 72 / 96 * 100);
  return (
    <div className={`page-viewer ${expanded ? "expanded" : ""}`} data-testid={testId ?? "page-viewer"}>
      {heading ? <div className="viewer-heading">{heading}</div> : null}
      <div className="viewer-toolbar" role="toolbar" aria-label={`${title} controls`}>
        <div className="vt-group">
          <button className="btn outline small" aria-label="Previous page" disabled={current <= 1} onClick={() => goto(current - 1)}>‹</button>
          <span className="vt-page" aria-live="polite">Page <b>{current}</b> of {pages.length || "–"}</span>
          <button className="btn outline small" aria-label="Next page" disabled={current >= pages.length} onClick={() => goto(current + 1)}>›</button>
        </div>
        <div className="vt-group">
          <button className="btn outline small" aria-label="Zoom out" onClick={() => stepZoom(-1)}>−</button>
          <span className="vt-zoom" aria-label="Zoom level">{percent}%</span>
          <button className="btn outline small" aria-label="Zoom in" onClick={() => stepZoom(1)}>+</button>
          <button className={`btn small ${zoom === "fit" ? "dark" : "outline"}`} onClick={() => setZoom("fit")}>Fit width</button>
          <button className={`btn small ${zoom === 1 ? "dark" : "outline"}`} onClick={() => setZoom(1)}>Actual size</button>
          <button className="btn outline small" aria-pressed={expanded} onClick={() => setExpanded((v) => !v)}>{expanded ? "Exit full screen" : "Full screen"}</button>
        </div>
      </div>
      {note ? <div className="viewer-note">{note}</div> : null}
      <div className="viewer-scroll" ref={scroller} tabIndex={0} aria-label={title}>
        {error ? <p className="stage-note">{error}</p> : !doc ? <p className="stage-note">Opening the page preview…</p>
          : pages.filter((p) => !onlyPages || onlyPages.includes(p.page)).map((p) => <ViewerPage key={p.page} doc={doc} size={p} scale={scale} highlights={highlights.filter((h) => h.page === p.page)} active={activeHighlight} />)}
      </div>
    </div>
  );
}

function ViewerPage({ doc, size, scale, highlights, active }: { doc: Doc; size: { page: number; width: number; height: number }; scale: number; highlights: Highlight[]; active?: string | null }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const host = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(size.page <= 2);
  useEffect(() => {
    const el = host.current; if (!el) return;
    const io = new IntersectionObserver((e) => { if (e.some((x) => x.isIntersecting)) setVisible(true); }, { rootMargin: "600px" });
    io.observe(el); return () => io.disconnect();
  }, []);
  useEffect(() => {
    if (!visible) return;
    let cancelled = false; let task: { cancel: () => void } | null = null;
    (async () => {
      const page = await doc.getPage(size.page);
      const ratio = Math.min(2, window.devicePixelRatio || 1);
      const viewport = page.getViewport({ scale: scale * ratio });
      const el = canvas.current; if (!el || cancelled) return;
      el.width = viewport.width; el.height = viewport.height;
      const ctx = el.getContext("2d"); if (!ctx) return;
      const render = page.render({ canvasContext: ctx, viewport });
      task = render; try { await render.promise; } catch { /* cancelled */ }
    })();
    return () => { cancelled = true; task?.cancel(); };
  }, [doc, size.page, scale, visible]);
  const w = size.width * scale, h = size.height * scale;
  return (
    <div className="viewer-page" data-page={size.page} ref={host} style={{ width: w, height: h }}>
      <canvas ref={canvas} style={{ width: w, height: h }} />
      {highlights.map((hl) => {
        const [x0, y0, x1, y1] = hl.bbox;
        return <div key={hl.id} data-hl={hl.id} className={`hl hl-${hl.kind} ${active === hl.id ? "active" : ""}`} title={hl.label}
          style={{ left: `${(x0 / size.width) * 100}%`, top: `${((size.height - y1) / size.height) * 100}%`, width: `${((x1 - x0) / size.width) * 100}%`, height: `${((y1 - y0) / size.height) * 100}%` }} />;
      })}
      <span className="viewer-page-no">{size.page}</span>
    </div>
  );
}

