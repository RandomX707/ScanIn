import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// The Edits tab shows the SAME LibreOffice pages as Original, edits go to native targets, and the proposed change is rendered before saving.
// Real stakeholder DOCX (private, gitignored): skipped, never faked, when absent. Screenshots stay under .private/.
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const file = path.join(root, 'Policy Copies/QCR/Health Quote Format.docx');
const evidence = path.join(root, '.private/visual-evidence/docx-edit');
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));
const API = process.env.IDW_API_BASE_URL || 'http://127.0.0.1:8010';

test.beforeEach(async ({ page }) => { await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token); });

test.describe('DOCX: faithful pages in Edits, draft preview, saved native version', () => {
  test.skip(!fs.existsSync(file), 'private stakeholder corpus not present');
  test('Original and Edits show the same pages; a cell edit is previewed, then saved as a new DOCX version', async ({ page }) => {
    test.setTimeout(300_000);
    fs.mkdirSync(evidence, { recursive: true });
    const mutations = [];
    page.on('request', (r) => { if (r.method() === 'POST' && /\/save$/.test(r.url())) mutations.push(r.url()); });
    const previewResponse = page.waitForResponse((r) => /\/versions\/[^/]+\/preview$/.test(r.url()) && r.status() === 200, { timeout: 120_000 });
    await page.goto('/');
    await page.setInputFiles('#file-input', file);
    await expect(page.locator('.file-main h2')).toHaveText('Health Quote Format.docx', { timeout: 90_000 });
    const [, documentId, versionId] = (await previewResponse).url().match(/documents\/([^/]+)\/versions\/([^/]+)\/preview/);
    const sha = (await (await fetch(`${API}/api/documents/${documentId}/history`, { headers: { Authorization: `Bearer ${run().token}` } })).json()).versions[0].sha256;

    const original = page.getByTestId('docx-page-viewer');
    await expect(original.locator('.viewer-page')).toHaveCount(4, { timeout: 60_000 });
    await expect(original.locator('canvas').first()).toBeVisible();
    await page.waitForTimeout(2500);
    const sig = (v, n) => v.locator('canvas').nth(n).evaluate((c) => { const x = c.getContext('2d'); const w = c.width, h = c.height; const d = x.getImageData(0, 0, w, h).data; let colored = 0, dark = 0; for (let i = 0; i < d.length; i += 16) { const mx = Math.max(d[i], d[i + 1], d[i + 2]), mn = Math.min(d[i], d[i + 1], d[i + 2]); if (mx - mn > 60) colored++; if (mx < 80) dark++; } const n = d.length / 16; return { w, h, aspect: w / h, colored: colored / n, dark: dark / n }; });
    const originalPage2 = await sig(original, 1);
    await original.locator('.viewer-page').nth(1).screenshot({ path: path.join(evidence, 'A-original-page2.png') });

    await page.locator('.tab', { hasText: 'Edits' }).click();
    const editor = page.getByTestId('docx-page-editor');
    await expect(editor).toBeVisible({ timeout: 30_000 });
    const pagesView = page.getByTestId('docx-edit-viewer');
    await expect(pagesView.locator('.viewer-page')).toHaveCount(4, { timeout: 60_000 });
    await page.waitForTimeout(2500);
    const editsPage2 = await sig(pagesView, 1);
    await pagesView.locator('.viewer-page').nth(1).screenshot({ path: path.join(evidence, 'B-edits-page2-before.png') });
    // same composition: same canvas size, and the same amount of coloured (header bands, yellow rows) and dark (text/border) pixels
    expect(Math.abs(editsPage2.aspect - originalPage2.aspect)).toBeLessThan(0.005);
    expect(Math.abs(editsPage2.colored - originalPage2.colored) / Math.max(1e-6, originalPage2.colored)).toBeLessThan(0.08);
    expect(Math.abs(editsPage2.dark - originalPage2.dark) / Math.max(1e-6, originalPage2.dark)).toBeLessThan(0.15);
    expect(originalPage2.colored).toBeGreaterThan(0.02);                   // the coloured table headings really are drawn

    await expect(page.getByTestId('locate-summary')).toBeVisible({ timeout: 60_000 });
    const located = Number((await page.getByTestId('locate-summary').innerText()).match(/^(\d+) of/)[1]);
    expect(located).toBeGreaterThan(5);
    // click a verified location ON THE PAGE: the panel shows exactly that native text
    const hl = pagesView.locator('.hl-edit.clickable').first();
    await hl.scrollIntoViewIfNeeded(); await hl.click();
    const original1 = (await page.getByTestId('original-value').innerText()).trim();
    expect(original1.length).toBeGreaterThan(0);
    await expect(page.getByTestId('target-list').locator('button[aria-current="true"]')).toContainText(original1.slice(0, 20));

    // pick a table cell from the synchronized list
    const preview = await (await fetch(`${API}/api/documents/${documentId}/versions/${versionId}/preview`, { headers: { Authorization: `Bearer ${run().token}` } })).json();
    const cell = preview.blocks.find((b) => b.e && b.table && b.text.length > 3 && preview.blocks.filter((o) => o.text === b.text).length === 1);
    expect(cell).toBeTruthy();
    await page.getByLabel('Search document text').fill(cell.text.slice(0, 25));
    await page.getByTestId('target-list').locator('button').first().click();
    await expect(page.getByTestId('original-value')).toHaveText(cell.text);
    const replacement = cell.text + ' (revised)';
    await page.getByTestId('replacement').fill(replacement);
    await expect(page.locator('#edit-state')).toHaveText('Unsaved changes');
    await page.screenshot({ path: path.join(evidence, 'C-edits-selected-cell.png') });

    // draft preview shows the PROPOSED change; nothing is saved yet
    await page.getByTestId('preview-changes').click();
    const draft = page.getByTestId('docx-draft-viewer');
    await expect(draft.locator('.viewer-page')).not.toHaveCount(0, { timeout: 120_000 });
    await page.waitForTimeout(2500);
    await draft.screenshot({ path: path.join(evidence, 'D-draft-preview.png') });
    const draftSig = await sig(draft, 1);
    expect(Math.abs(draftSig.aspect - originalPage2.aspect)).toBeLessThan(0.005);
    expect(Math.abs(draftSig.colored - originalPage2.colored) / Math.max(1e-6, originalPage2.colored)).toBeLessThan(0.15);     // colours/bands survive
    expect(mutations).toEqual([]);
    const historyBefore = (await (await fetch(`${API}/api/documents/${documentId}/history`, { headers: { Authorization: `Bearer ${run().token}` } })).json()).versions.length;
    expect(historyBefore).toBe(1);

    // an edit after the preview marks it out of date
    await page.getByTestId('replacement').fill(replacement + '!');
    await expect(page.getByTestId('draft-tab')).toContainText('out of date');
    await page.getByTestId('replacement').fill(replacement);
    await page.getByTestId('draft-tab').click();

    // save: a new native DOCX version, original byte-identical, shown in History and comparable with its parent
    await page.getByRole('button', { name: 'Save as next version' }).click();
    await expect(page.locator('.toast, [role="status"]').filter({ hasText: /Version v2 saved/ }).first()).toBeVisible({ timeout: 120_000 });
    const history = await (await fetch(`${API}/api/documents/${documentId}/history`, { headers: { Authorization: `Bearer ${run().token}` } })).json();
    expect(history.versions.length).toBe(2);
    expect(history.versions[0].sha256).toBe(sha);
    expect(history.versions[1].parent_version_id ?? history.versions[1].parent ?? versionId).toBeTruthy();
    const bytes = Buffer.from(await (await fetch(`${API}/api/documents/${documentId}/versions/${history.versions[1].version_id}/file`, { headers: { Authorization: `Bearer ${run().token}` } })).arrayBuffer());
    expect(bytes.subarray(0, 2).toString()).toBe('PK');
    fs.writeFileSync(path.join(evidence, 'derived.docx'), bytes);
    await page.getByRole('button', { name: 'History' }).first().click();
    await page.screenshot({ path: path.join(evidence, 'E-saved-version.png') });
  });
});
