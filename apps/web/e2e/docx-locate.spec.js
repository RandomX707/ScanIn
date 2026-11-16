import { test, expect } from '@playwright/test';
import { locateBlocks, pageIsUpright } from '../src/lib/docxLocate.ts';

// Pure unit tests of the DOCX locator (no browser, no server): a position is advertised only when the native block maps unambiguously to the intended rendered text.
const item = (str, x, y, w = null, h = 10) => ({ str, x, y, w: w ?? str.length * 5, h });
const page = (...items) => ({ page: 1, items });
const where = (blocks, pages) => locateBlocks(blocks, pages);

test('exact single match is located with the item box', () => {
  const r = where([{ id: 'b1', text: 'Sum insured' }], [page(item('Sum insured', 50, 700))]);
  expect(r.b1).toEqual({ page: 1, bbox: [50, 700, 105, 710] });
});

test('REGRESSION: text that exists only as a substring of a longer rendered element is not located', () => {
  const r = where([{ id: 'b1', text: 'Yes' }], [page(item('Yes please confirm', 50, 700))]);
  expect(r.b1).toBeUndefined();
  const r2 = where([{ id: 'b1', text: 'Premium' }], [page(item('Annual Premium Schedule', 50, 700), item('Total', 300, 700))]);
  expect(r2.b1).toBeUndefined();
});

test('a block that is a substring of ANOTHER BLOCK\'s rendered text is not located while its own rendering is absent', () => {
  const blocks = [{ id: 'short', text: 'Total' }, { id: 'long', text: 'Total payable' }];
  const r = where(blocks, [page(item('Total payable', 50, 700))]);
  expect(r.long).toBeDefined(); expect(r.short).toBeUndefined();
});

test('repeated labels and identical table-cell values are never located', () => {
  const blocks = [{ id: 'a', text: 'Yes' }, { id: 'b', text: 'Yes' }];
  expect(where(blocks, [page(item('Yes', 50, 700), item('Yes', 200, 700))])).toEqual({});
  // one native block, but the label is rendered twice (for example a repeating header): ambiguous
  expect(where([{ id: 'a', text: 'Header' }], [page(item('Header', 50, 800)), { page: 2, items: [item('Header', 50, 800)] }])).toEqual({});
});

test('the same text twice in the rendering for a single block is ambiguous even if one is inside a longer element', () => {
  const r = where([{ id: 'a', text: 'Total' }], [page(item('Total', 50, 700), item('Total', 200, 700, 25))]);
  expect(r.a).toBeUndefined();
});

test('split text runs: a paragraph drawn as several adjacent items is located as one box', () => {
  const r = where([{ id: 'p', text: 'Premium payable annually' }], [page(item('Premium ', 50, 700, 40), item('payable', 90, 700, 35), item(' annually', 125, 700, 45))]);
  expect(r.p).toEqual({ page: 1, bbox: [50, 700, 170, 710] });
});

test('wrapped paragraph over two lines is located as the union of its lines', () => {
  const r = where([{ id: 'p', text: 'Premium payable annually in advance' }], [page(item('Premium payable annually', 50, 700, 120), item('in advance', 50, 688, 50))]);
  expect(r.p).toEqual({ page: 1, bbox: [50, 688, 170, 710] });
});

test('REGRESSION: adjacent text on the same line that is not part of the block makes the match ambiguous', () => {
  // "Sum" + "Insured" is rendered, but the line continues with "Details": the block is only a prefix of what is drawn there
  const r = where([{ id: 'p', text: 'Sum Insured' }], [page(item('Sum', 50, 700, 15), item('Insured', 68, 700, 35), item('Details', 106, 700, 35))]);
  expect(r.p).toBeUndefined();
  // a separate cell far to the right on the same line is fine
  const r2 = where([{ id: 'p', text: 'Sum Insured' }], [page(item('Sum', 50, 700, 15), item('Insured', 68, 700, 35), item('1000', 300, 700, 20))]);
  expect(r2.p).toBeDefined();
});

test('whitespace and NFKC normalisation: spacing differences do not matter, but different characters do', () => {
  expect(where([{ id: 'p', text: 'Sum   insured value' }], [page(item('Sum insured value', 50, 700))]).p).toBeDefined();
  expect(where([{ id: 'p', text: 'Sum insured value' }], [page(item('Sum insured valve', 50, 700))]).p).toBeUndefined();
});

test('merged cell: a wide cell rendered once is located; its text repeated in the document is not', () => {
  expect(where([{ id: 'm', text: 'Base Quote Comparison' }], [page(item('Base Quote Comparison', 100, 650, 300))]).m).toBeDefined();
  expect(where([{ id: 'm', text: 'Base Quote Comparison' }], [page(item('Base Quote Comparison', 100, 650, 300), item('Base Quote Comparison', 100, 300, 300))]).m).toBeUndefined();
});

test('text absent from the rendering, or empty, is not located', () => {
  expect(where([{ id: 'p', text: 'Not drawn anywhere' }], [page(item('Something else', 50, 700))])).toEqual({});
  expect(where([{ id: 'p', text: '   ' }], [page(item('x', 50, 700))])).toEqual({});
});

test('a block split across two pages is not given a single box', () => {
  const r = where([{ id: 'p', text: 'Premium payable' }], [page(item('Premium', 50, 50)), { page: 2, items: [item('payable', 50, 780)] }]);
  expect(r.p).toBeUndefined();
});

test('non-upright or offset pages are never trusted', () => {
  expect(pageIsUpright(0, [0, 0, 595, 842])).toBe(true);
  expect(pageIsUpright(90, [0, 0, 595, 842])).toBe(false);
  expect(pageIsUpright(0, [10, 0, 595, 842])).toBe(false);
  expect(pageIsUpright(0, [0, 5, 595, 842])).toBe(false);
});

test('one unreadable page anywhere means uniqueness cannot be proven: nothing is located', () => {
  const r = where([{ id: 'p', text: 'Sum insured' }], [page(item('Sum insured', 50, 700)), { page: 2, items: [], unreadable: true }]);
  expect(r).toEqual({});
});
