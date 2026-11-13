import { useEffect, useMemo, useRef, useState } from "react";
import type { Cell, DocxBlock, PdfField, Preview, SaveBody } from "../lib/api";
import { SheetGrid } from "./SheetGrid";
import { SheetWorkspace } from "../components/SheetWorkspace";

type OnBody = (body: SaveBody | null) => void;

/* ------------------------------------------------------------------------------------------------ text */

function insert(area: HTMLTextAreaElement, start: number, end: number, text: string, selectFrom = 0, selectTo = text.length) {
  area.focus();
  area.setSelectionRange(start, end);
  // execCommand keeps the browser's undo stack; fall back to a direct edit when it is unavailable.
  if (!document.execCommand("insertText", false, text)) {
    area.setRangeText(text, start, end, "end");
    area.dispatchEvent(new Event("input", { bubbles: true }));
  }
  area.setSelectionRange(start + selectFrom, start + selectTo);
}

function lineBounds(value: string, start: number, end: number): [number, number] {
  const from = value.lastIndexOf("\n", start - 1) + 1;
  const next = value.indexOf("\n", end);
  return [from, next === -1 ? value.length : next];
}

function applyFormat(area: HTMLTextAreaElement, kind: string, action: string) {
  const { value, selectionStart: s, selectionEnd: e } = area;
  const selected = value.slice(s, e);
  if (kind === "md") {
    if (action === "bold" || action === "italic") {
      const mark = action === "bold" ? "**" : "*";
      const inner = selected || (action === "bold" ? "bold text" : "italic text");
      insert(area, s, e, `${mark}${inner}${mark}`, mark.length, mark.length + inner.length);
      return;
    }
    const [from, to] = lineBounds(value, s, e);
    const lines = value.slice(from, to).split("\n").map((line) => {
      const bare = line.replace(/^(#{1,6}\s+|[-*]\s+)/, "");
      return action === "h2" ? `## ${bare}` : action === "list" ? `- ${bare}` : bare;
    });
    insert(area, from, to, lines.join("\n"), 0, lines.join("\n").length);
    return;
  }
  // HTML
  const wrap = (open: string, close: string, placeholder: string) => {
    const inner = selected || placeholder;
    insert(area, s, e, `${open}${inner}${close}`, open.length, open.length + inner.length);
  };
  if (action === "bold") wrap("<strong>", "</strong>", "bold text");
  else if (action === "italic") wrap("<em>", "</em>", "italic text");
  else if (action === "underline") wrap("<u>", "</u>", "underlined text");
  else if (action === "h2") wrap("<h2>", "</h2>", "Heading");
  else if (action === "p") wrap("<p>", "</p>", "Paragraph");
  else if (action === "list") {
    const items = (selected || "List item").split("\n").filter(Boolean).map((line) => `  <li>${line}</li>`).join("\n");
    insert(area, s, e, `<ul>\n${items}\n</ul>`, 0, items.length + 9);
  }
}

export function TextEditor({ preview, formatting, onBody, onReset }: { preview: Preview; formatting: boolean; onBody: OnBody; onReset: () => void }) {
  const original = preview.text ?? "";
  const [value, setValue] = useState(original);
  const area = useRef<HTMLTextAreaElement>(null);
  const kind = preview.kind ?? "txt";
  const change = (next: string) => { setValue(next); onBody(next !== original ? { text: next } : null); };
  const tool = (action: string) => (e: React.MouseEvent) => { e.preventDefault(); if (area.current) applyFormat(area.current, kind, action); };
  return (
    <>
      {formatting && (
        <div className="editor-toolbar" role="toolbar" aria-label="Editor formatting">
          <button className="tool-button" onMouseDown={tool("bold")} title="Bold"><b>B</b></button>
          <button className="tool-button" onMouseDown={tool("italic")} title="Italic"><i>I</i></button>
          {kind === "html" && <button className="tool-button" onMouseDown={tool("underline")} title="Underline"><u>U</u></button>}
          <span className="toolbar-sep" />
          <button className="tool-button" onMouseDown={tool("h2")} title="Heading">H2</button>
          <button className="tool-button" onMouseDown={tool("p")} title="Paragraph">¶</button>
          <button className="tool-button" onMouseDown={tool("list")} title="Bulleted list">☷</button>
          <span className="toolbar-sep" />
          <button className="tool-button" onMouseDown={(e) => { e.preventDefault(); area.current?.focus(); document.execCommand("undo"); }} title="Undo">↶</button>
          <button className="tool-button" onClick={onReset} title="Restore starting version">↺</button>
        </div>
      )}
      <textarea ref={area} className={`editable plain-text ${formatting ? "" : "no-toolbar"}`} aria-label="Editable working copy"
        spellCheck value={value} onChange={(e) => change(e.target.value)} rows={18} />
    </>
  );
}

/* ------------------------------------------------------------------------------------------------ grid */

export function GridEditor({ preview, onBody }: { preview: Preview; onBody: OnBody }) {
  const sheets = useMemo(() => (preview.sheets ?? []).filter((s) => s.rows.length), [preview]);
  const [active, setActive] = useState(0);
  const [edits, setEdits] = useState<Record<string, { cell: NonNullable<Cell>; value: string }>>({});
  const update = (cell: NonNullable<Cell>, value: string) => {
    const next = { ...edits };
    if (value === cell.v) delete next[cell.id]; else next[cell.id] = { cell, value };
    setEdits(next);
    const changes = Object.values(next).map(({ cell: c, value: v }) => ({ target_id: c.id, fingerprint: c.fp, value: v }));
    onBody(changes.length ? { changes } : null);
  };
  if (!sheets.length) return <div className="empty"><h3>No values found</h3><p>This file has no cells to edit.</p></div>;
  const sheet = sheets[Math.min(active, sheets.length - 1)];
  if (sheets.every((s) => s.layout)) {
    return (
      <>
        <div className="editable grid-editor no-toolbar" role="region" aria-label="Editable cells">
          <SheetWorkspace sheets={sheets} styles={preview.styles ?? []} edits={edits} onEdit={update} label="Editable workbook" testId="sheet-editor" />
        </div>
      </>
    );
  }
  return (
    <>
      {sheets.length > 1 && (
        <div className="sheet-pills">{sheets.map((s, i) => <button key={s.name} className={`version-pill ${i === active ? "active" : ""}`} onClick={() => setActive(i)}>{s.name}</button>)}</div>
      )}
      <div className="editable grid-editor no-toolbar" role="region" aria-label="Editable cells">
        {sheet.layout
          ? <SheetGrid sheet={sheet} styles={preview.styles ?? []} edits={edits} onEdit={update} />
          : <table>
              <tbody>
                {sheet.rows.map((row, r) => (
                  <tr key={r}>
                    {row.map((cell, c) => {
                      const Tag = r === 0 ? "th" : "td";
                      if (!cell) return <Tag key={c} />;
                      if (!cell.e) return <Tag key={c}><div className="cell-locked" title={cell.r ?? "Locked"}>{cell.v}</div></Tag>;
                      const changed = cell.id in edits;
                      return (
                        <Tag key={c} className={changed ? "cell-changed" : ""}>
                          <input className="cell-input" aria-label={`${sheet.name} ${r + 1}:${c + 1}`} data-cell={cell.id}
                            value={changed ? edits[cell.id].value : cell.v} onChange={(e) => update(cell, e.target.value)} />
                        </Tag>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>}
      </div>
      {sheet.truncated && <p className="grid-note">Showing the first rows and columns of this sheet.</p>}
    </>
  );
}

/* ------------------------------------------------------------------------------------------------ docx */

export function DocxEditor({ preview, onBody }: { preview: Preview; onBody: OnBody }) {
  const blocks = preview.blocks ?? [];
  const edits = useRef<Record<string, { block: DocxBlock; value: string }>>({});
  const report = () => {
    const changes = Object.values(edits.current).map(({ block, value }) => ({ target_id: block.target_id!, fingerprint: block.fp!, value }));
    onBody(changes.length ? { changes } : null);
  };
  const track = (block: DocxBlock) => (e: React.FormEvent<HTMLElement>) => {
    const value = e.currentTarget.textContent ?? "";
    if (value === block.text) delete edits.current[block.id]; else edits.current[block.id] = { block, value };
    report();
  };
  const noEnter = (e: React.KeyboardEvent) => { if (e.key === "Enter") e.preventDefault(); };
  const text = (block: DocxBlock) => block.e
    ? <span contentEditable="plaintext-only" suppressContentEditableWarning role="textbox" aria-label={block.text.slice(0, 60)} data-target={block.target_id ?? undefined}
      onInput={track(block)} onKeyDown={noEnter} spellCheck>{block.text}</span>
    : <span className="locked-text" title={block.r ?? "Read-only text"} data-readonly="true">{block.text}</span>;
  const nodes: React.ReactNode[] = [];
  for (let i = 0; i < blocks.length; i++) {
    const b = blocks[i];
    if (b.table) {
      const index = b.table.index; const cells: DocxBlock[] = [];
      while (i < blocks.length && blocks[i].table?.index === index) cells.push(blocks[i++]);
      i--;
      const rows = new Map<number, DocxBlock[]>();
      for (const c of cells) rows.set(c.table!.row, [...(rows.get(c.table!.row) ?? []), c]);
      nodes.push(<table key={b.id}><tbody>{[...rows.entries()].sort((x, y) => x[0] - y[0]).map(([r, row]) => <tr key={r}>{row.map((c) => <td key={c.id}>{text(c)}</td>)}</tr>)}</tbody></table>);
    } else if (b.heading) {
      const Tag = (`h${Math.min(3, b.heading + 1)}`) as "h2";
      nodes.push(<Tag key={b.id}>{text(b)}</Tag>);
    } else nodes.push(<p key={b.id}>{text(b)}</p>);
  }
  return <div className="editable docx-editor no-toolbar" aria-label="Editable document">{nodes.length ? nodes : <p>No editable text available.</p>}</div>;
}

/* ------------------------------------------------------------------------------------------------ pdf */

type PdfLib = typeof import("pdfjs-dist");

export function PdfEditor({ preview, fetchBytes, onBody }: { preview: Preview; fetchBytes: () => Promise<ArrayBuffer>; onBody: OnBody }) {
  const fields = useMemo(() => preview.fields ?? [], [preview]);
  const pages = preview.pages ?? [];
  const [values, setValues] = useState<Record<string, string | boolean>>({});
  const [pdf, setPdf] = useState<import("pdfjs-dist").PDFDocumentProxy | null>(null);
  const lib = useRef<PdfLib | null>(null);

  useEffect(() => {
    let live = true;
    (async () => {
      const pdfjs = await import("pdfjs-dist");
      const worker = (await import("pdfjs-dist/build/pdf.worker.min.mjs?url")).default;
      pdfjs.GlobalWorkerOptions.workerSrc = worker;
      lib.current = pdfjs;
      const doc = await pdfjs.getDocument({ data: new Uint8Array(await fetchBytes()) }).promise;
      if (live) setPdf(doc);
    })().catch(() => undefined);
    return () => { live = false; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const current = (f: PdfField): string | boolean => (f.id in values ? values[f.id] : f.type === "checkbox" ? Boolean(f.value) : String(f.value ?? ""));
  const set = (f: PdfField, v: string | boolean) => {
    const original = f.type === "checkbox" ? Boolean(f.value) : String(f.value ?? "");
    const next = { ...values };
    if (v === original) delete next[f.id]; else next[f.id] = v;
    setValues(next);
    const changes = fields.filter((x) => x.id in next).map((x) => ({ target_id: x.id, fingerprint: x.fp, value: next[x.id] }));
    onBody(changes.length ? { changes } : null);
  };

  return (
    <div className="pdf-editor" role="region" aria-label="Fillable PDF">
      {pdf ? pages.map((page) => (
        <PdfPage key={page.page} pdf={pdf} lib={lib.current!} page={page}
          fields={fields.filter((f) => f.widgets.some((w) => w.page === page.page))}
          current={current} onChange={set} changed={(f) => f.id in values} />
      )) : <p className="stage-note">Opening the form…</p>}
    </div>
  );
}

function PdfPage({ pdf, lib, page, fields, current, onChange, changed }: {
  pdf: import("pdfjs-dist").PDFDocumentProxy; lib: PdfLib; page: { page: number; width: number; height: number };
  fields: PdfField[]; current: (f: PdfField) => string | boolean;
  onChange: (f: PdfField, v: string | boolean) => void; changed: (f: PdfField) => boolean;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const p = await pdf.getPage(page.page);
      const viewport = p.getViewport({ scale: 1.6 });
      const el = canvas.current; if (!el || cancelled) return;
      el.width = viewport.width; el.height = viewport.height;
      const context = el.getContext("2d"); if (!context) return;
      // Annotations are not drawn: the editable controls below replace the form widgets.
      await p.render({ canvasContext: context, viewport, annotationMode: lib.AnnotationMode.DISABLE }).promise;
    })().catch(() => undefined);
    return () => { cancelled = true; };
  }, [pdf, lib, page.page]);
  return (
    <div className="pdf-page" style={{ aspectRatio: `${page.width} / ${page.height}` }}>
      <canvas ref={canvas} aria-label={`Page ${page.page}`} />
      {fields.flatMap((f) => f.widgets.filter((w) => w.page === page.page).map((w, i) => {
        const [x1, y1, x2, y2] = w.rect;
        const style: React.CSSProperties = {
          left: `${(Math.min(x1, x2) / page.width) * 100}%`, top: `${((page.height - Math.max(y1, y2)) / page.height) * 100}%`,
          width: `${(Math.abs(x2 - x1) / page.width) * 100}%`, height: `${(Math.abs(y2 - y1) / page.height) * 100}%`,
          fontSize: `${Math.max(1.6, (Math.abs(y2 - y1) * 0.55 / page.width) * 100)}cqw`,
        };
        const key = `${f.id}:${i}`;
        const common = { className: `pdf-field ${changed(f) ? "changed" : ""}`, style, disabled: !f.e, title: f.e ? f.name : f.r ?? "Locked", "aria-label": f.name, "data-field": f.name };
        if (f.type === "checkbox") return <input key={key} {...common} type="checkbox" checked={Boolean(current(f))} onChange={(e) => onChange(f, e.target.checked)} />;
        if (f.type === "radio") return <input key={key} {...common} type="radio" name={f.id} aria-label={`${f.name}: ${w.state ?? ""}`} checked={current(f) === (w.state ?? "")} onChange={() => onChange(f, w.state ?? "")} />;
        if (f.type === "choice") return <select key={key} {...common} value={String(current(f))} onChange={(e) => onChange(f, e.target.value)}>{f.options.map((o) => <option key={o} value={o}>{o}</option>)}</select>;
        return <input key={key} {...common} type="text" value={String(current(f))} onChange={(e) => onChange(f, e.target.value)} />;
      }))}
    </div>
  );
}


/* ------------------------------------------------------------------------------------------------ rendered Markdown / HTML */

export function RichEditor({ preview, onBody, onReset }: { preview: Preview; onBody: OnBody; onReset: () => void }) {
  const box = useRef<HTMLDivElement>(null);
  const initial = useRef("");
  useEffect(() => {
    if (!box.current) return;
    box.current.innerHTML = preview.html ?? "<p></p>";
    initial.current = box.current.innerHTML;
  }, [preview.html]);
  const run = (command: string, value?: string) => (e: React.MouseEvent) => {
    e.preventDefault();
    box.current?.focus();
    document.execCommand(command, false, value);
    changed();
  };
  const changed = () => {
    const html = box.current?.innerHTML ?? "";
    onBody(html !== initial.current ? { html } : null);
  };
  return (
    <>
      <div className="editor-toolbar" role="toolbar" aria-label="Editor formatting">
        <button className="tool-button" onMouseDown={run("bold")} title="Bold"><b>B</b></button>
        <button className="tool-button" onMouseDown={run("italic")} title="Italic"><i>I</i></button>
        <button className="tool-button" onMouseDown={run("underline")} title="Underline"><u>U</u></button>
        <span className="toolbar-sep" />
        <button className="tool-button" onMouseDown={run("formatBlock", "h2")} title="Heading">H2</button>
        <button className="tool-button" onMouseDown={run("formatBlock", "p")} title="Paragraph">¶</button>
        <button className="tool-button" onMouseDown={run("insertUnorderedList")} title="Bulleted list">☷</button>
        <span className="toolbar-sep" />
        <button className="tool-button" onMouseDown={run("undo")} title="Undo">↶</button>
        <button className="tool-button" onMouseDown={run("redo")} title="Redo">↷</button>
        <button className="tool-button" onClick={onReset} title="Restore starting version">↺</button>
      </div>
      <div ref={box} className="editable" contentEditable suppressContentEditableWarning role="textbox" aria-multiline="true"
        aria-label="Editable working copy" spellCheck onInput={changed} />
    </>
  );
}
