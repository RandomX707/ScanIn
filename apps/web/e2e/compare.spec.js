import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Real stakeholder files (private, gitignored). Skipped, never faked, when absent.
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const corpus = process.env.CHANNEL360_CORPUS_DIR || path.join(root, 'Policy Copies');
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));
const car = path.join(corpus, 'CAR Policy copy.pdf');
const xlsx = path.join(corpus, 'QCR', 'CAR-QCR.xlsx');
const docx = path.join(corpus, 'QCR', 'Health Quote Format.docx');

test.beforeEach(async ({ page }) => {
  await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token);
});

async function upload(page, file) {
  await page.goto('/');
  await page.setInputFiles('#file-input', file);
  await expect(page.locator('.file-main h2')).toHaveText(path.basename(file), { timeout: 90_000 });
}
async function saveNext(page, expected) {
  await page.getByRole('button', { name: 'Save as next version' }).click();
  await expect(page.locator('.toast')).toContainText('saved inside', { timeout: 120_000 });
  await expect(page.locator('.file-meta')).toContainText(`Version ${expected} of ${expected}`);   // the new version is open
}

test.describe('Version comparison', () => {
  test.skip(!fs.existsSync(car) || !fs.existsSync(xlsx) || !fs.existsSync(docx), 'private stakeholder corpus not present');

  test('PDF: v1 vs v2 highlights the real changed line on both sides; navigation and version pickers work', async ({ page }) => {
    await upload(page, car);
    await page.locator('.tab', { hasText: 'Edits' }).click();
    const runs = page.locator('.pdf-run:not([data-locked])');
    await expect(runs.first()).toBeVisible({ timeout: 60_000 });
    const labels = await runs.evaluateAll((els) => els.map((el) => ({ id: el.dataset.run, label: el.getAttribute('aria-label') })));
    const multiWord = (l) => /^Edit text: .{14,36}$/.test(l.label) && l.label.replace('Edit text: ', '').includes(' ');
    const picks = labels.filter(multiWord).slice(0, 2);
    const edit = async (pick) => {
      const oldText = pick.label.replace('Edit text: ', '');
      await page.locator(`[data-run="${pick.id}"]`).click();
      const input = page.locator('.pdf-run-input');
      await input.fill(oldText.split(' ').slice(0, -1).join(' '));
      await input.press('Enter');
      await expect(page.locator(`.pdf-run.changed[data-run="${pick.id}"]`)).toBeVisible({ timeout: 60_000 });
    };
    await edit(picks[0]);
    await saveNext(page, 2);                                // v2
    await page.locator('.tab', { hasText: 'Edits' }).click();
    await expect(runs.first()).toBeVisible({ timeout: 60_000 });
    const labels2 = await runs.evaluateAll((els) => els.map((el) => ({ id: el.dataset.run, label: el.getAttribute('aria-label') })));
    const second = labels2.find((l) => l.label === picks[1].label) || labels2.find((l) => multiWord(l) && l.label !== picks[0].label);
    await edit(second);
    await saveNext(page, 3);                                // v3 (from v2)

    // History > folder > Compare with parent
    await page.getByRole('button', { name: /All versions/ }).click();
    await page.locator('.version-item').filter({ hasText: 'v3' }).getByRole('button', { name: 'Compare with v2' }).click();
    await expect(page.getByTestId('compare-page')).toBeVisible();
    await expect(page.getByTestId('relation')).toContainText('v3 was saved directly from v2');
    await expect(page.getByTestId('change-summary')).toContainText('1 modified');
    await expect(page.locator('.change-row')).toHaveCount(1);
    await expect(page.getByTestId('viewer-base').locator('.hl-mod')).toHaveCount(1, { timeout: 60_000 });
    await expect(page.getByTestId('viewer-target').locator('.hl-mod')).toHaveCount(1);
    expect(await page.locator('.audit-lines').innerText()).toContain('by channel360-demo-owner');

    // cumulative v1 -> v3 via the pickers
    await page.getByLabel('Base version').selectOption({ label: 'v1 · Original' });
    await expect(page.getByTestId('relation')).toContainText('descends from v1 through v1 → v2 → v3');
    await expect(page.locator('.change-row')).toHaveCount(2);
    await expect(page.getByTestId('change-summary')).toContainText('2 modified');
    await page.getByRole('button', { name: 'Next change' }).click();
    await expect(page.locator('.change-row.active')).toHaveCount(1);
    await expect(page.locator('.compare-nav')).toContainText('Change 2 of 2');
    await page.getByRole('button', { name: 'Previous change' }).click();
    await expect(page.locator('.compare-nav')).toContainText('Change 1 of 2');
    // changed-content mode keeps only pages that changed
    await page.getByRole('button', { name: 'Changed content' }).click();
    await expect(page.getByTestId('viewer-base').locator('.viewer-page')).toHaveCount(1, { timeout: 30_000 });
    // swapping works and reports the reverse relation
    await page.getByRole('button', { name: 'Swap versions' }).click();
    await expect(page.getByTestId('relation')).toContainText('earlier ancestor');
  });

  test('XLSX: edited cells are marked in both grids and clicking a change navigates to the cell', async ({ page }) => {
    await upload(page, xlsx);
    await page.locator('.tab', { hasText: 'Edits' }).click();
    const grid = page.locator('.grid-editor .sheet-fidelity');
    await expect(grid).toBeVisible();
    const inputs = grid.locator('.cell-input');
    let edited = 0;
    for (const i of [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]) {
      const v = await inputs.nth(i).inputValue();
      if (v && /[A-Za-z]/.test(v) && v.length < 50 && edited < 2) { await inputs.nth(i).fill(v + ' revised'); edited++; }
    }
    expect(edited).toBe(2);
    await saveNext(page, 2);
    await page.getByRole('button', { name: /Compare with v1/ }).click();
    await expect(page.getByTestId('change-summary')).toContainText('2 modified');
    await expect(page.locator('.change-row')).toHaveCount(2);
    await expect(page.getByTestId('sheet-base').locator('.sg-mark-mod')).toHaveCount(2, { timeout: 30_000 });
    await expect(page.getByTestId('sheet-target').locator('.sg-mark-mod')).toHaveCount(2);
    await page.locator('.change-row').nth(1).click();
    await expect(page.getByTestId('sheet-target').locator('.sg-active')).toHaveCount(1);
    const label = await page.locator('.change-row.active .change-loc').innerText();
    const addr = label.split('!')[1];
    await expect(page.getByTestId('sheet-target').locator(`.sg-active[data-addr="${addr}"]`)).toHaveCount(1);
  });

  test('DOCX: paragraph and table edits show word-level before/after and both page renderings', async ({ page }) => {
    await upload(page, docx);
    await page.locator('.tab', { hasText: 'Edits' }).click();
    const editable = page.locator('.docx-editor [data-target]');
    await expect(editable.first()).toBeVisible({ timeout: 30_000 });
    await editable.nth(3).click();
    await page.keyboard.press('End');
    await page.keyboard.type(' revised');
    await saveNext(page, 2);
    await page.getByRole('button', { name: /Compare with v1/ }).click();
    await expect(page.getByTestId('change-summary')).toContainText('1 modified');
    await expect(page.locator('.change-after .diff-add')).toContainText('revised');
    await expect(page.getByTestId('viewer-base').locator('canvas').first()).toBeVisible({ timeout: 60_000 });
    await expect(page.getByTestId('viewer-target').locator('canvas').first()).toBeVisible({ timeout: 60_000 });
    await expect(page.locator('.limits')).toContainText('not compared');
  });
});
