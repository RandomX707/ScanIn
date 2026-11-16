/** Verified block -> rendered-position mapping for the DOCX page editor.
 *
 *  The paginated LibreOffice rendering carries no native identifiers, so a block is placed on a page only when ALL of these hold:
 *   1. exactly one block of the document has that (whitespace/NFKC-normalised) text;
 *   2. that text occurs exactly once anywhere in the rendered text (a repeated label, or the text also appearing inside another element, is ambiguous);
 *   3. the single occurrence starts at the beginning of a rendered text item and ends at the end of one (it is whole items, never a fragment of a longer element);
 *   4. no other rendered item touches it on the same line (text that continues the line means the block is only part of what is drawn there);
 *   5. it lies on one page, and the page is upright with its box at the origin.
 *  Anything else stays UNLOCATED and is reached through the synchronized list. No location is ever guessed. */
export type TextItem = { str: string; x: number; y: number; w: number; h: number };
/** `unreadable`: the page could not be trusted (rotated or offset box), so its text is unknown. */
export type PageText = { page: number; items: TextItem[]; unreadable?: boolean };
export type Located = { page: number; bbox: [number, number, number, number] };

const squeeze = (s: string) => s.replace(/\s+/g, "").normalize("NFKC");
/** Same-line items closer than this many text heights are treated as touching (word spacing is ~0.25 em; separate table cells are much further apart). */
const TOUCH = 0.6;

export const pageIsUpright = (rotate: number, view: number[]) => rotate === 0 && view[0] === 0 && view[1] === 0;

type Flat = { page: number; item: TextItem; start: number; end: number };   // [start, end) offsets in the squeezed document string

export function locateBlocks(blocks: { id: string; text: string }[], pages: PageText[]): Record<string, Located> {
  if (pages.some((p) => p.unreadable)) return {};
  let doc = "";
  const flat: Flat[] = [];
  for (const p of pages) {
    for (const item of p.items) {
      const t = squeeze(item.str);
      if (!t) continue;                                          // whitespace-only items draw nothing
      flat.push({ page: p.page, item, start: doc.length, end: doc.length + t.length });
      doc += t;
    }
  }
  const keyed = blocks.map((b) => ({ id: b.id, key: squeeze(b.text) }));
  const sameText = new Map<string, number>();
  for (const b of keyed) sameText.set(b.key, (sameText.get(b.key) ?? 0) + 1);
  const found: Record<string, Located> = {};
  for (const { id, key } of keyed) {
    if (!key || sameText.get(key) !== 1) continue;                                   // (1)
    const first = doc.indexOf(key);
    if (first < 0 || doc.indexOf(key, first + 1) >= 0) continue;                      // (2) absent, or occurs again anywhere (including inside other text)
    const last = first + key.length;
    const covered = flat.filter((f) => f.end > first && f.start < last);
    if (!covered.length || covered[0].start !== first || covered[covered.length - 1].end !== last) continue;   // (3) whole items only
    const pageNo = covered[0].page;
    if (covered.some((c) => c.page !== pageNo)) continue;                             // (5) never a single box for a block split over pages
    const inRange = new Set(covered.map((c) => c.item));
    const touching = flat.some((o) => o.page === pageNo && !inRange.has(o.item) && covered.some((c) => {   // (4)
      const h = Math.max(c.item.h, o.item.h, 1);
      if (Math.abs(o.item.y - c.item.y) >= TOUCH * h) return false;                 // different line
      const gapRight = o.item.x - (c.item.x + c.item.w), gapLeft = c.item.x - (o.item.x + o.item.w);
      return gapRight <= TOUCH * h && gapLeft <= TOUCH * h;                          // neither clearly to the right nor clearly to the left
    }));
    if (touching) continue;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const it of inRange) { x0 = Math.min(x0, it.x); y0 = Math.min(y0, it.y); x1 = Math.max(x1, it.x + it.w); y1 = Math.max(y1, it.y + it.h); }
    found[id] = { page: pageNo, bbox: [x0, y0, x1, y1] };
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
    if (!pageIsUpright(page.rotate, page.view)) { out.push({ page: n, items: [], unreadable: true }); continue; }
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
