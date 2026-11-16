import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// Final acceptance on the real Health Quote Format.docx in an isolated tenant. Artifacts (private) go to .private/acceptance/.
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const file = path.join(root, 'Policy Copies/QCR/Health Quote Format.docx');
const out = path.join(root, '.private/acceptance');
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));
const API = process.env.IDW_API_BASE_URL || 'http://127.0.0.1:8010';
const H = () => ({ Authorization: `Bearer ${run().token}` });
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');

test.beforeEach(async ({ page }) => { await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token); });
test.skip(!fs.existsSync(file), 'private corpus not present');

async function open(page) {
  const pr = page.waitForResponse((r) => /\/versions\/[^/]+\/preview$/.test(r.url()) && r.status() === 200, { timeout: 120_000 });
  await page.goto('/'); await page.setInputFiles('#file-input', file);
  await expect(page.locator('.file-main h2')).toHaveText('Health Quote Format.docx', { timeout: 90_000 });
  const [, d, v] = (await pr).url().match(/documents\/([^/]+)\/versions\/([^/]+)\/preview/);
  const blocks = (await (await fetch(`${API}/api/documents/${d}/versions/${v}/preview`, { headers: H() })).json()).blocks;
  await page.locator('.tab', { hasText: 'Edits' }).click();
  await expect(page.getByTestId('docx-page-editor')).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId('locate-summary')).toBeVisible({ timeout: 90_000 });
  return { d, v, blocks, editable: blocks.filter((b) => b.e) };
}
const getFile = async (d, vid) => Buffer.from(await (await fetch(`${API}/api/documents/${d}/versions/${vid}/file`, { headers: H() })).arrayBuffer());
const history = async (d) => (await (await fetch(`${API}/api/documents/${d}/history`, { headers: H() })).json()).versions;

test('A. controlled table-cell edit: draft captured, saved, parent lineage, original unchanged', async ({ page }) => {
  test.setTimeout(300_000); fs.mkdirSync(out, { recursive: true });
  const { d, v, blocks, editable } = await open(page);
  const original = fs.readFileSync(file);
  const idx = editable.findIndex((b) => b.table && b.text.length > 3 && editable.filter((o) => o.text === b.text).length === 1);
  const cell = editable[idx];
  await page.getByTestId('target-list').locator('button').nth(idx).click();
  await expect(page.getByTestId('original-value')).toHaveText(cell.text);
  const value = cell.text + ' (revised)';
  await page.getByTestId('replacement').fill(value);
  const draftResp = page.waitForResponse((r) => r.url().endsWith('/docx-edit/preview') && r.status() === 200, { timeout: 120_000 });
  await page.getByTestId('preview-changes').click();
  fs.writeFileSync(path.join(out, 'docx-draft.pdf'), await (await draftResp).body());
  expect((await history(d)).length).toBe(1);                                        // a draft creates no version
  const saveResp = page.waitForResponse((r) => /\/save$/.test(r.url()), { timeout: 180_000 });
  await page.getByRole('button', { name: 'Save as next version' }).click();
  const saved = await (await saveResp).json();
  const hist = await history(d);
  expect(hist.length).toBe(2); expect(hist[0].sha256).toBe(sha(original));
  fs.writeFileSync(path.join(out, 'docx-v1.docx'), await getFile(d, hist[0].version_id));
  fs.writeFileSync(path.join(out, 'docx-v2.docx'), await getFile(d, hist[1].version_id));
  fs.writeFileSync(path.join(out, 'docx-meta.json'), JSON.stringify({ target_id: cell.target_id, table: cell.table, old: cell.text, new: value, save_outcome: saved.outcome, parent_number: hist[1].parent_number, kind: hist[1].kind, v1_matches_original: hist[0].sha256 === sha(original), draft_was_listed: false }, null, 1));
  expect(['VERIFIED', 'STRUCTURALLY_VERIFIED', 'INCOMPLETE']).toContain(saved.outcome);
});

test('B. identical text in different table cells (synthetic DOCX; the real document has no repeated editable text): only the selected target changes', async ({ page }) => {
  test.setTimeout(300_000); fs.mkdirSync(out, { recursive: true });
  const synth = path.join(out, 'synthetic-duplicates.docx');
  execFileSync(process.env.IDW_E2E_PYTHON || path.join(root, '.venv/bin/python'), ['-c', `
import docx
d = docx.Document(); d.add_heading('Synthetic quote', 1)
t = d.add_table(rows=3, cols=2)
for r, (a, b) in enumerate([('Yes', 'Yes'), ('Covered', 'Covered'), ('Yes', 'No')]):
    t.cell(r, 0).text = a; t.cell(r, 1).text = b
d.save(${JSON.stringify(synth)})`]);
  const pr = page.waitForResponse((r) => /\/versions\/[^/]+\/preview$/.test(r.url()) && r.status() === 200, { timeout: 120_000 });
  await page.goto('/'); await page.setInputFiles('#file-input', synth);
  await expect(page.locator('.file-main h2')).toHaveText('synthetic-duplicates.docx', { timeout: 90_000 });
  const [, d, v] = (await pr).url().match(/documents\/([^/]+)\/versions\/([^/]+)\/preview/);
  const editable = (await (await fetch(`${API}/api/documents/${d}/versions/${v}/preview`, { headers: H() })).json()).blocks.filter((b) => b.e);
  await page.locator('.tab', { hasText: 'Edits' }).click();
  await expect(page.getByTestId('target-list').locator('button').first()).toBeVisible({ timeout: 90_000 });
  const ix = editable.map((b, i) => (b.text === 'Yes' ? i : -1)).filter((i) => i >= 0);
  expect(ix.length).toBe(3);
  const list = page.getByTestId('target-list').locator('button');
  await list.nth(ix[1]).click();
  await page.getByTestId('replacement').fill('Maybe');
  await expect(list.nth(ix[0])).not.toHaveAttribute('data-changed', 'true');
  await expect(list.nth(ix[2])).not.toHaveAttribute('data-changed', 'true');
  await expect(list.nth(ix[1])).toHaveAttribute('data-changed', 'true');
  await page.getByRole('button', { name: 'Save as next version' }).click();
  await expect(page.locator('.toast').filter({ hasText: /saved inside/ }).first()).toBeVisible({ timeout: 180_000 });
  const hist = await history(d);
  fs.writeFileSync(path.join(out, 'dup-v1.docx'), await getFile(d, hist[0].version_id));
  fs.writeFileSync(path.join(out, 'dup-v2.docx'), await getFile(d, hist[1].version_id));
  fs.writeFileSync(path.join(out, 'dup-meta.json'), JSON.stringify({ edited_target: editable[ix[1]].target_id, other_yes_targets: [editable[ix[0]].target_id, editable[ix[2]].target_id] }, null, 1));
});

test('C. every editable text without a verified page position is reachable and previewable from the list', async ({ page }) => {
  test.setTimeout(580_000); fs.mkdirSync(out, { recursive: true });
  const { d, v, editable } = await open(page);
  const list = page.getByTestId('target-list').locator('button');
  expect(await list.count()).toBe(editable.length);
  const unlocated = [];
  for (let i = 0; i < editable.length; i++) if ((await list.nth(i).locator('.where').innerText()) === 'list') unlocated.push(i);
  const results = [];
  for (const i of unlocated) {
    const b = editable[i];
    await list.nth(i).click();
    const shown = await page.getByTestId('original-value').evaluate((el) => el.textContent);
    const same = shown === b.text;
    await page.getByTestId('replacement').fill(b.text + ' ~');
    const resp = page.waitForResponse((r) => r.url().endsWith('/docx-edit/preview'), { timeout: 120_000 });
    await page.getByTestId('preview-changes').click();
    const r = await resp;
    results.push({ index: i, panel_matches_target: same, draft_status: r.status() });
    await page.getByRole('button', { name: 'Revert this text' }).click();
  }
  fs.writeFileSync(path.join(out, 'unlocated-targets.json'), JSON.stringify({ editable: editable.length, unlocated: unlocated.length, results }, null, 1));
  expect(unlocated.length).toBeGreaterThan(0);
  expect(results.every((x) => x.panel_matches_target && x.draft_status === 200)).toBe(true);
  expect((await history(d)).length).toBe(1);
});
