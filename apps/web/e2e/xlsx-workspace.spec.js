import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Real stakeholder workbooks (private, gitignored); skipped, never faked, when absent. Evidence screenshots stay under .private/.
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const manifestPath = path.join(root, '.private/inventory/manifest.json');
const manifest = fs.existsSync(manifestPath) ? JSON.parse(fs.readFileSync(manifestPath, 'utf8')) : null;
const seen = new Set();
const books = manifest ? manifest.occurrences.filter((o) => o.kind === 'xlsx' && !seen.has(o.sha256) && seen.add(o.sha256)) : [];
const extracted = (o) => path.join(root, '.private/corpus', o.source.replace(/\.zip$/, ''), o.path);
const evidence = path.join(root, '.private/visual-evidence/xlsx');
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));

test.beforeEach(async ({ page }) => {
  await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token);
});
test.beforeAll(() => fs.mkdirSync(evidence, { recursive: true }));

test.describe('Excel workspace on the real RFQ/QCR workbooks', () => {
  test.skip(!manifest || books.length !== 9, 'private stakeholder corpus not present');
  for (const o of books) {
    const name = path.basename(o.path);
    test(`${name}: headers, tabs, zoom, fit, scroll, full screen, formulas, unsaved edits survive`, async ({ page }) => {
      const file = extracted(o);
      test.skip(!fs.existsSync(file), 'extracted fixture missing');
      await page.goto('/');
      await page.setInputFiles('#file-input', file);
      await expect(page.locator('.file-main h2')).toHaveText(name, { timeout: 90_000 });
      const viewer = page.getByTestId('sheet-viewer');
      await expect(viewer.locator('.sheet-fidelity')).toBeVisible({ timeout: 30_000 });
      const grid = viewer.locator('.sheet-fidelity');
      const scroller = viewer.locator('.sheet-scroll');

      // spreadsheet chrome
      expect(await viewer.locator('.sg-colhdr').count()).toBeGreaterThan(3);
      expect(await viewer.locator('.sg-rowhdr').count()).toBeGreaterThan(5);
      expect(await viewer.locator('.sheet-tab').count()).toBeGreaterThanOrEqual(1);
      await expect(viewer.locator('.formula-bar')).toBeVisible();

      // formulas: every one shown, marked and locked (summed over all worksheets)
      let formulas = 0;
      const tabs = viewer.locator('.sheet-tab');
      for (let i = 0; i < await tabs.count(); i++) { await tabs.nth(i).click(); formulas += await viewer.locator('.sg-formula').count(); }
      expect(formulas).toBe(o.formulas);
      await tabs.first().click();

      // zoom: grid scales proportionally and controls report it
      await viewer.getByRole('button', { name: 'Actual size' }).click();
      const w100 = (await grid.boundingBox()).width;
      await viewer.getByRole('button', { name: 'Zoom out' }).click();
      const w90 = (await grid.boundingBox()).width;
      expect(Math.abs(w90 / w100 - 0.9)).toBeLessThan(0.02);
      await expect(viewer.locator('.vt-zoom')).toHaveText('90%');
      await viewer.getByRole('button', { name: 'Zoom in' }).click();
      await viewer.getByRole('button', { name: 'Zoom in' }).click();
      expect(Math.abs((await grid.boundingBox()).width / w100 - 1.25)).toBeLessThan(0.02);

      // fit width: the used range fits the visible area (never below the readability floor)
      await viewer.getByRole('button', { name: 'Fit width' }).click();
      const zoomText = await viewer.locator('.vt-zoom').innerText();
      expect(parseInt(zoomText, 10)).toBeGreaterThanOrEqual(50);

      // scrolling: content scrolls and the sticky headers stay put
      await viewer.getByRole('button', { name: 'Actual size' }).click();
      const scrollable = await scroller.evaluate((el) => ({ w: el.scrollWidth > el.clientWidth, h: el.scrollHeight > el.clientHeight }));
      if (scrollable.h) {
        await scroller.evaluate((el) => { el.scrollTop = 300; });
        const hdrTop = await viewer.locator('.sg-colhdr').first().evaluate((el) => el.getBoundingClientRect().top);
        const boxTop = await scroller.evaluate((el) => el.getBoundingClientRect().top);
        expect(Math.abs(hdrTop - boxTop)).toBeLessThan(3);
        await scroller.evaluate((el) => { el.scrollTop = 0; });
      }
      if (scrollable.w) { await scroller.evaluate((el) => { el.scrollLeft = 200; }); expect(await scroller.evaluate((el) => el.scrollLeft)).toBeGreaterThan(0); await scroller.evaluate((el) => { el.scrollLeft = 0; }); }

      // selection + full screen keep the same cell
      const firstCell = viewer.locator('td.sg-cell[data-addr]').filter({ hasNot: page.locator('input') }).nth(0);
      await viewer.locator('td.sg-cell[data-addr]').nth(2).click();
      const selectedBefore = await viewer.locator('.sg-selected').first().getAttribute('data-addr');
      await page.screenshot({ path: path.join(evidence, `${name}-normal.png`) });
      await viewer.getByRole('button', { name: 'Full screen' }).click();
      const expanded = page.locator('[data-testid="sheet-viewer"].expanded');
      await expect(expanded).toBeVisible();
      const box = await expanded.boundingBox();
      const vp = page.viewportSize();
      expect(box.width).toBeGreaterThanOrEqual(vp.width - 2); expect(box.height).toBeGreaterThanOrEqual(vp.height - 2);
      expect(await viewer.locator('.sg-selected').first().getAttribute('data-addr')).toBe(selectedBefore);
      await viewer.getByRole('button', { name: 'Fit width' }).click();
      await page.screenshot({ path: path.join(evidence, `${name}-expanded.png`) });
      await page.keyboard.press('Escape');
      await expect(page.locator('[data-testid="sheet-viewer"].expanded')).toHaveCount(0);
      void firstCell;

      // Edits: an unsaved edit survives expand/collapse and saves natively
      await page.locator('.tab', { hasText: 'Edits' }).click();
      const editor = page.getByTestId('sheet-editor');
      await expect(editor.locator('.sheet-fidelity')).toBeVisible();
      const inputs = editor.locator('.cell-input');
      let target = null;
      for (let i = 0; i < Math.min(await inputs.count(), 20); i++) { const v = await inputs.nth(i).inputValue(); if (v && /[A-Za-z]/.test(v) && v.length < 50) { target = inputs.nth(i); break; } }
      expect(target, 'a text cell exists').not.toBeNull();
      const before = await target.inputValue();
      await target.fill(before + ' e2e');
      await expect(page.locator('#edit-state')).toHaveText('Unsaved changes');
      await editor.getByRole('button', { name: 'Full screen' }).click();
      await expect(page.locator('[data-testid="sheet-editor"].expanded')).toBeVisible();
      await expect(page.locator('#edit-state')).toHaveText('Unsaved changes');
      await page.keyboard.press('Escape');
      expect(await editor.locator('.cell-changed .cell-input').first().inputValue()).toBe(before + ' e2e');
      await page.getByRole('button', { name: 'Save as next version' }).click();
      await expect(page.locator('.toast')).toContainText('saved inside', { timeout: 120_000 });
      await expect(page.locator('.file-meta')).toContainText('Version 2 of 2');
    });
  }
});
