import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// History -> open a version -> Compare -> choose the other version -> understand -> jump to each change -> back to the workspace,
// on BRANCHING histories of real stakeholder files (private corpus; skipped, never faked, when absent).
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const corpus = process.env.CHANNEL360_CORPUS_DIR || path.join(root, 'Policy Copies');
const docx = path.join(corpus, 'QCR/Health Quote Format.docx'); const xlsx = path.join(corpus, 'QCR/GMC QCR Format.XLSX');
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));
test.beforeEach(async ({ page }) => { await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token); });

async function upload(page, file) { await page.goto('/'); await page.setInputFiles('#file-input', file); await expect(page.locator('.file-main h2')).toHaveText(path.basename(file), { timeout: 90_000 }); }
async function saveNext(page, n) {
  await page.getByRole('button', { name: 'Save as next version' }).click();
  await expect(page.locator('.toast')).toContainText('saved inside', { timeout: 120_000 });
  await expect(page.locator('.file-meta')).toContainText(`Version ${n} of ${n}`);
}
const typeAtEnd = async (page, loc, text) => { await loc.click(); await loc.evaluate((el) => { const r = document.createRange(); r.selectNodeContents(el); r.collapse(false); const s = window.getSelection(); s.removeAllRanges(); s.addRange(r); }); await page.keyboard.type(text); };

test.describe('Comparing branching histories in the browser', () => {
  test.skip(!fs.existsSync(docx) || !fs.existsSync(xlsx), 'private stakeholder corpus not present');

  test('DOCX: v1 -> v2 and v1 -> v3 (a branch): compare v2 with v3, jump to each change, return to the workspace', async ({ page }) => {
    await upload(page, docx);
    await page.locator('.tab', { hasText: 'Edits' }).click();
    const editable = page.locator('.docx-editor [data-target]'); await expect(editable.first()).toBeVisible({ timeout: 30_000 });
    await typeAtEnd(page, editable.nth(3), ' alpha'); await saveNext(page, 2);                       // v2 from v1
    await page.locator('.version-pill', { hasText: 'v1' }).click();                                    // open the ORIGINAL again
    await expect(page.locator('.file-meta')).toContainText('Version 1 of 2');
    await page.locator('.tab', { hasText: 'Edits' }).click(); await expect(editable.first()).toBeVisible({ timeout: 30_000 });
    await typeAtEnd(page, editable.nth(8), ' beta'); await saveNext(page, 3);                         // v3 from v1 (not from v2)
    await page.getByRole('button', { name: /All versions/ }).click();
    await expect(page.locator('.version-item').nth(2)).toContainText('Derived from v1');
    await page.locator('.version-item').nth(2).getByLabel('Compare v3 with another version').selectOption({ label: 'v2' });
    await expect(page.getByTestId('relation')).toContainText('separate branches');
    await expect(page.getByTestId('relation')).toContainText('v2 → v1 → v3');
    await expect(page.getByTestId('change-summary')).toContainText('2 modified');
    await expect(page.getByTestId('completeness')).toContainText('COMPLETE');
    await expect(page.locator('.change-row')).toHaveCount(2);
    // each change is located on its page in the viewers and can be jumped to
    for (let i = 0; i < 2; i++) {
      await page.locator('.change-row').nth(i).click();
      await expect(page.locator('.change-row.active')).toHaveCount(1);
      await expect(page.locator('[data-testid="viewer-target"] .hl.active, [data-testid="viewer-base"] .hl.active').first()).toBeVisible({ timeout: 30_000 });
    }
    await page.getByRole('button', { name: 'Next change' }).click(); await expect(page.locator('.compare-nav')).toContainText('of 2');
    // back to the workspace of either side
    await page.getByRole('button', { name: /^Open v3/ }).click();
    await expect(page.locator('.file-meta')).toContainText('Version 3 of 3');
    await expect(page.locator('.crumb')).toContainText('v3');
  });

  test('XLSX: a branch history compares completely; jump to each changed cell; return to the workspace', async ({ page }) => {
    await upload(page, xlsx);
    const editCell = async (index) => {
      await page.locator('.tab', { hasText: 'Edits' }).click();
      const inputs = page.getByTestId('sheet-editor').locator('.cell-input'); await expect(inputs.first()).toBeVisible();
      let n = 0;
      for (let i = 0; i < Math.min(await inputs.count(), 60); i++) { const v = await inputs.nth(i).inputValue(); if (v && /[A-Za-z]/.test(v) && v.length < 40) { if (n++ === index) { await inputs.nth(i).fill(v + ' x'); return; } } }
    };
    await editCell(0); await saveNext(page, 2);
    await page.locator('.version-pill', { hasText: 'v1' }).click(); await expect(page.locator('.file-meta')).toContainText('Version 1 of 2');
    await editCell(3); await saveNext(page, 3);
    await page.getByRole('button', { name: /All versions/ }).click();
    await page.locator('.version-item').nth(2).getByLabel('Compare v3 with another version').selectOption({ label: 'v2' });
    await expect(page.getByTestId('relation')).toContainText('separate branches');
    await expect(page.getByTestId('completeness')).toContainText('COMPLETE', { timeout: 60_000 });
    await expect(page.getByTestId('change-summary')).toContainText('2 modified');
    await expect(page.locator('.change-row')).toHaveCount(2);
    for (let i = 0; i < 2; i++) {
      await page.locator('.change-row').nth(i).click();
      const addr = (await page.locator('.change-row.active .change-loc').innerText()).split('!')[1];
      await expect(page.getByTestId('sheet-target').locator(`.sg-cell.sg-active[data-addr="${addr}"]`)).toBeVisible({ timeout: 20_000 });
      await expect(page.getByTestId('sheet-base').locator(`.sg-cell.sg-active[data-addr="${addr}"]`)).toBeVisible({ timeout: 20_000 });
    }
    await page.getByRole('button', { name: /^Open v2/ }).click();
    await expect(page.locator('.file-meta')).toContainText('Version 2 of 3');
  });
});
