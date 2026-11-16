import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// DOCX page editor: every editable target stays reachable in a long document, and clicking a highlighted position can only ever select the block it was verified for.
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const out = path.join(root, '.private/acceptance');
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));
const API = process.env.IDW_API_BASE_URL || 'http://127.0.0.1:8010';
const H = () => ({ Authorization: `Bearer ${run().token}` });
const PY = process.env.IDW_E2E_PYTHON || path.join(root, '.venv/bin/python');
const real = path.join(root, 'Policy Copies/QCR/Health Quote Format.docx');

test.beforeEach(async ({ page }) => { await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token); });

function synth(name, code) {
  fs.mkdirSync(out, { recursive: true });
  const file = path.join(out, name);
  execFileSync(PY, ['-c', `import docx\nd = docx.Document()\n${code}\nd.save(${JSON.stringify(file)})`]);
  return file;
}
async function openDoc(page, file, title) {
  const pr = page.waitForResponse((r) => /\/versions\/[^/]+\/preview$/.test(r.url()) && r.status() === 200, { timeout: 120_000 });
  await page.goto('/'); await page.setInputFiles('#file-input', file);
  await expect(page.locator('.file-main h2')).toHaveText(title, { timeout: 90_000 });
  const [, d, v] = (await pr).url().match(/documents\/([^/]+)\/versions\/([^/]+)\/preview/);
  const blocks = (await (await fetch(`${API}/api/documents/${d}/versions/${v}/preview`, { headers: H() })).json()).blocks;
  await page.locator('.tab', { hasText: 'Edits' }).click();
  await expect(page.getByTestId('locate-summary')).toBeVisible({ timeout: 90_000 });
  return { d, v, blocks };
}

test('a long document: the list says how many texts there are and every editable target stays reachable', async ({ page }) => {
  test.setTimeout(240_000);
  const file = synth('long450.docx', 'for i in range(450):\n    d.add_paragraph("Clause number %d is payable annually." % i)');
  const { blocks } = await openDoc(page, file, 'long450.docx');
  const editable = blocks.filter((b) => b.e); expect(editable.length).toBe(450);
  const list = page.getByTestId('target-list').locator('button');
  await expect(page.getByTestId('list-count')).toContainText('Showing 200 of 450');
  expect(await list.count()).toBe(200);
  await page.getByTestId('list-more').click(); expect(await list.count()).toBe(400);
  await page.getByTestId('list-more').click();
  await expect(page.getByTestId('list-count')).toContainText('Showing all 450');
  expect(await list.count()).toBe(450);
  // the very last one can be selected and edited, and search narrows across the whole list (not just the visible part)
  await page.getByLabel('Search document text').fill('Clause number 449 ');
  await expect(list).toHaveCount(1);
  await list.first().click();
  await expect(page.getByTestId('original-value')).toHaveText(editable[449].text);
  await page.getByTestId('replacement').fill(editable[449].text + ' (edited)');
  await expect(page.locator('#edit-state')).toHaveText('Unsaved changes');
});

test('substring, repeated and identical texts get no page position; every advertised position selects exactly its own block', async ({ page }) => {
  test.setTimeout(240_000);
  const file = synth('ambiguous.docx', `d.add_paragraph("Yes please confirm the proposal details below.")
d.add_paragraph("Unique statement about the premium schedule.")
t = d.add_table(rows=2, cols=2)
for r, row in enumerate([("Yes", "Yes"), ("Covered", "Yes")]):
    for c, v in enumerate(row):
        t.cell(r, c).text = v
d.add_paragraph("Total")
d.add_paragraph("Total payable amount is shown here.")`);
  const { blocks } = await openDoc(page, file, 'ambiguous.docx');
  const byId = new Map(blocks.map((b) => [b.id, b]));
  const ids = await page.locator('.hl-edit.clickable').evaluateAll((els) => els.map((e) => e.getAttribute('data-hl')));
  const located = ids.map((i) => byId.get(i).text);
  expect(located).toContain('Unique statement about the premium schedule.');
  expect(located).not.toContain('Yes');                                            // identical cell values (three of them) and the start of a longer sentence
  expect(located).toContain('Covered');                                            // a unique cell value is fine
  expect(located).not.toContain('Total');                                          // its text also begins a longer rendered line
  // every advertised highlight selects exactly its own native block
  for (const id of ids) {
    await page.locator(`.hl-edit.clickable[data-hl="${id}"]`).first().click();
    await expect(page.getByTestId('original-value')).toHaveText(byId.get(id).text);
  }
});

test('real Health Quote document: every highlight selects exactly the native block it was verified for', async ({ page }) => {
  test.skip(!fs.existsSync(real), 'private corpus not present');
  test.setTimeout(300_000);
  const { blocks } = await openDoc(page, real, 'Health Quote Format.docx');
  const byId = new Map(blocks.map((b) => [b.id, b]));
  const ids = await page.locator('.hl-edit.clickable').evaluateAll((els) => [...new Set(els.map((e) => e.getAttribute('data-hl')))]);
  expect(ids.length).toBeGreaterThan(5);
  let checked = 0;
  for (const id of ids) {
    const hl = page.locator(`.hl-edit.clickable[data-hl="${id}"]`).first();
    await hl.scrollIntoViewIfNeeded(); await hl.click();
    const shown = await page.getByTestId('original-value').evaluate((el) => el.textContent);
    expect(shown, `highlight ${id}`).toBe(byId.get(id).text);
    checked++;
  }
  fs.writeFileSync(path.join(out, 'highlight-click-check.json'), JSON.stringify({ editable_blocks: blocks.filter((b) => b.e).length, highlights_checked: checked, all_match: true }, null, 1));
});
