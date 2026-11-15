/** Verified block -> rendered-position mapping for the DOCX page editor.
 *
 *  The paginated LibreOffice rendering carries no native identifiers, so a block is placed on the page only when its text occurs EXACTLY ONCE in the
 *  rendered text and exactly one block of the document has that text. Anything else (repeated labels, identical cells, header/footer text repeated on
 *  every page, text the renderer reshaped) stays UNLOCATED and is reached through the synchronized list instead. No location is ever guessed. */
export type TextItem = { str: string; x: number; y: number; w: number; h: number };
export type PageText = { page: number; items: TextItem[] };
export type Located = { page: number; bbox: [number, number, number, number] };

const squeeze = (s: string) => s.replace(/\s+/g, "").normalize("NFKC");

export function locateBlocks(blocks: { id: string; text: string }[], pages: PageText[]): Record<string, Located> {
  let doc = "";
  const owner: { page: number; item: TextItem }[] = [];       // per squeezed character
  for (const p of pages) {
    for (const item of p.items) {
      const t = squeeze(item.str);
      for (const _ of t) owner.push({ page: p.page, item });  // eslint-disable-line @typescript-eslint/no-unused-vars
      doc += t;
    }
  }
  const sameText = new Map<string, number>();
  const keyed = blocks.map((b) => ({ id: b.id, key: squeeze(b.text) }));
  for (const b of keyed) sameText.set(b.key, (sameText.get(b.key) ?? 0) + 1);
  const found: Record<string, Located> = {};
  for (const { id, key } of keyed) {
    if (!key || sameText.get(key) !== 1) continue;
    const first = doc.indexOf(key);
    if (first < 0 || doc.indexOf(key, first + 1) >= 0) continue;                // absent, or occurs again anywhere (incl. inside other text)
    const covered = owner.slice(first, first + [...key].length);
    const page = covered[0].page;
    if (covered.some((c) => c.page !== page)) continue;                         // a block split across pages is not given a single box
    const items = new Set(covered.map((c) => c.item));
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const it of items) { x0 = Math.min(x0, it.x); y0 = Math.min(y0, it.y); x1 = Math.max(x1, it.x + it.w); y1 = Math.max(y1, it.y + it.h); }
    found[id] = { page, bbox: [x0, y0, x1, y1] };
  }
  return found;
}

export async function readPageText(bytes: ArrayBuffer): Promise<PageText[]> {
  const pdfjs = await import("pdfjs-dist");
  pdfjs.GlobalWorkerOptions.workerSrc = (await import("pdfjs-dist/build/pdf.worker.min.mjs?url")).default;
  const doc = await pdfjs.getDocument({ data: new Uint8Array(bytes.slice(0)) }).promise;
  const out: PageText[] = [];
  for (let n = 1; n <= doc.numPages; n++) {
    const page = await doc.getPage(n);
    // Positions are only claimed for an upright page whose box starts at the origin; anything else stays unverified (list only).
    if (page.rotate !== 0 || page.view[0] !== 0 || page.view[1] !== 0) { out.push({ page: n, items: [] }); continue; }
    const content = await page.getTextContent();
    const items: TextItem[] = [];
    for (const raw of content.items) {
      const it = raw as { str?: string; transform?: number[]; width?: number; height?: number };
      if (!it.str || !it.transform) continue;
      items.push({ str: it.str, x: it.transform[4], y: it.transform[5], w: it.width ?? 0, h: it.height || Math.abs(it.transform[3]) });
    }
    out.push({ page: n, items });
  }
  return out;
}
