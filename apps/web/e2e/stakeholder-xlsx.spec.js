import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Private acceptance fixtures; skipped (never faked) when the extracted corpus / manifest is absent.
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const manifestPath = path.join(root, '.private/inventory/manifest.json');
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));
const manifest = fs.existsSync(manifestPath) ? JSON.parse(fs.readFileSync(manifestPath, 'utf8')) : null;
const seen = new Set();
const books = manifest ? manifest.occurrences.filter((o) => o.kind === 'xlsx' && !seen.has(o.sha256) && seen.add(o.sha256)) : [];
const extracted = (o) => path.join(root, '.private/corpus', o.source.replace(/\.zip$/, ''), o.path);

test.beforeEach(async ({ page }) => {
  await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token);
});

// Without the private evidence this file would register ZERO tests and vanish from the report. Say so explicitly instead (SKIPPED, with the prerequisite).
if (!manifest || books.length !== 9) {
  test('PREREQUISITE (private stakeholder corpus): Real XLSX workbooks', () => {
    test.skip(true, 'private stakeholder corpus/manifest not present (.private/ and Policy Copies/ are not part of the repository); these acceptance checks did NOT run');
  });
}

test.describe('Real XLSX workbooks look and edit like the template', () => {
  test.skip(!manifest || books.length !== 9, 'private stakeholder corpus not present');
  for (const o of books) {
    const name = path.basename(o.path);
    test(`${name}: merges, picture, editable grid, save`, async ({ page }) => {
      const file = extracted(o);
      test.skip(!fs.existsSync(file), 'extracted fixture missing');
      await page.goto('/');
      await page.setInputFiles('#file-input', file);
      await expect(page.locator('.file-main h2')).toHaveText(name, { timeout: 90_000 });
      // Original tab: real merged ranges and the sheet's picture
      const grid = page.locator('.sheet-fidelity').first();
      await expect(grid).toBeVisible({ timeout: 30_000 });
      const spans = await grid.locator('td[colspan], td[rowspan]').count();
      expect(spans).toBe(o.merged);                                   // every merged range is a real span
      const img = grid.locator('img');
      await expect(img).toHaveCount(1);
      expect(await img.evaluate((el) => el.complete && el.naturalWidth > 0)).toBe(true);
      // Edits tab: same layout, real editable cells
      await page.locator('.tab', { hasText: 'Edits' }).click();
      const editGrid = page.locator('.grid-editor .sheet-fidelity');
      await expect(editGrid).toBeVisible();
      expect(await editGrid.locator('td[colspan], td[rowspan]').count()).toBe(o.merged);
      const inputs = editGrid.locator('.cell-input');
      expect(await inputs.count()).toBeGreaterThan(5);
      let target = null;
      for (const i of [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) {
        const v = await inputs.nth(i).inputValue();
        if (v && /[A-Za-z]/.test(v) && v.length < 50) { target = inputs.nth(i); break; }
      }
      expect(target, 'a text cell exists').not.toBeNull();
      await target.fill((await target.inputValue()) + ' e2e');
      await expect(page.locator('#edit-state')).toHaveText('Unsaved changes');
      await page.getByRole('button', { name: 'Save as next version' }).click();
      await expect(page.locator('.toast')).toContainText('saved inside', { timeout: 120_000 });
      await expect(page.locator('.file-meta')).toContainText('Version 2 of 2');
      // the saved version keeps its layout
      const after = page.locator('.grid-editor .sheet-fidelity');
      await expect(after).toBeVisible();
      expect(await after.locator('td[colspan], td[rowspan]').count()).toBe(o.merged);
      await expect(after.locator('img')).toHaveCount(1);
    });
  }
});
