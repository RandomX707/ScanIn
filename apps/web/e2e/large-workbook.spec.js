import { test, expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Virtualised viewer, edit, save and COMPLETE comparison on large synthetic workbooks. Measurements go to .private/feature-results.
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const python = path.join(root, '.venv/bin/python');
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));
const sizes = (process.env.LARGE_ROWS || '2000,6000').split(',').map(Number);   // x30 columns = 60,000 and 180,000 cells
const results = path.join(root, '.private/feature-results/large-workbook-browser.json');
test.beforeEach(async ({ page }) => { await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token); });

for (const rows of sizes) {
  test(`${rows * 30} cells: virtual viewport, scrolling, goto, edit near and far, save, complete comparison`, async ({ page }) => {
    test.setTimeout(480_000);
    const file = path.join(root, `.private/e2e-output/large-${rows}.xlsx`); fs.mkdirSync(path.dirname(file), { recursive: true });
    execFileSync(python, [path.join(root, 'scripts/corpus/make_large_workbook.py'), String(rows), '30', file]);
    const m = { cells: rows * 30 }; const last = rows + 2;                        // header rows: 2
    const t0 = Date.now();
    await page.goto('/'); await page.setInputFiles('#file-input', file);
    await expect(page.locator('.file-main h2')).toHaveText(path.basename(file), { timeout: 120_000 }); m.import_to_workspace_s = (Date.now() - t0) / 1000;
    const viewer = page.locator('[data-testid="sheet-viewer"][data-virtual="true"]');
    const t1 = Date.now(); await expect(viewer.locator('.sg-cell').first()).toBeVisible({ timeout: 120_000 });
    await expect(viewer.locator('.sg-cell[data-addr="B10"]')).toBeVisible(); m.initial_viewport_s = (Date.now() - t1) / 1000;
    m.dom_cells_initial = await viewer.locator('.sg-cell').count(); expect(m.dom_cells_initial).toBeLessThan(4000);
    await expect(viewer.locator('.sheet-tab')).toHaveCount(2);

    // scrolling: jump to the far end, frozen header stays, then random jumps (time until the target row's cells are drawn)
    const scroller = viewer.locator('.vs-scroll');
    const t2 = Date.now(); await scroller.evaluate((el) => { el.scrollTop = el.scrollHeight; });
    await expect(viewer.locator(`.sg-cell[data-addr="A${last}"]`)).toBeVisible({ timeout: 20_000 }); m.jump_to_end_ms = Date.now() - t2;
    await expect(viewer.locator('.sg-cell[data-addr="A2"]')).toBeVisible();                // frozen header row is still on screen
    const frozenTop = await viewer.locator('.sg-cell[data-addr="A2"]').evaluate((el) => el.getBoundingClientRect().top - el.closest('.vs-scroll').getBoundingClientRect().top);
    expect(frozenTop).toBeLessThan(60);
    await scroller.evaluate((el) => { el.scrollLeft = el.scrollWidth; });                  // the formula column is the last one
    await expect.poll(async () => viewer.locator('.sg-formula').count(), { timeout: 15_000 }).toBeGreaterThan(0);   // formulas shown and marked (fx)
    await scroller.evaluate((el) => { el.scrollLeft = 0; });
    const jumps = [];
    for (const frac of [0.1, 0.77, 0.33, 0.9, 0.5, 0.05, 0.64, 0.21]) {
      const target = Math.max(3, Math.floor(rows * frac)); const t = Date.now();
      await scroller.evaluate((el, f) => { el.scrollTop = (el.scrollHeight - el.clientHeight) * f; }, frac);
      await expect.poll(async () => viewer.locator('.sg-cell .cell-text').first().evaluate((el) => el.textContent.length > 0), { timeout: 15_000 }).toBe(true);
      await expect(viewer.locator('.sg-cell[data-addr^="B"]').first()).toBeVisible(); jumps.push(Date.now() - t); void target;
    }
    jumps.sort((a, b) => a - b); m.random_jump_p50_ms = jumps[Math.floor(jumps.length / 2)]; m.random_jump_max_ms = jumps[jumps.length - 1];
    m.dom_cells_after_scroll = await viewer.locator('.sg-cell').count(); expect(m.dom_cells_after_scroll).toBeLessThan(4000);

    // direct navigation, zoom, full screen
    await viewer.getByLabel('Go to cell').fill(`D${Math.floor(rows / 2)}`); await viewer.getByRole('button', { name: 'Go', exact: true }).click();
    await expect(viewer.locator(`.sg-cell[data-addr="D${Math.floor(rows / 2)}"]`)).toBeVisible({ timeout: 15_000 });
    await viewer.getByRole('button', { name: 'Zoom out' }).click(); await viewer.getByRole('button', { name: 'Zoom in' }).click();
    await viewer.getByRole('button', { name: 'Full screen' }).click(); await expect(page.locator('[data-testid="sheet-viewer"].expanded')).toBeVisible(); await page.keyboard.press('Escape');

    // edit near the top and far below the first 16,000 cells
    await page.locator('.tab', { hasText: 'Edits' }).click();
    const editor = page.locator('[data-testid="sheet-editor"][data-virtual="true"]'); await expect(editor.locator('.sg-cell').first()).toBeVisible({ timeout: 60_000 });
    const editAt = async (addr, text) => {
      await editor.getByLabel('Go to cell').fill(addr); await editor.getByRole('button', { name: 'Go', exact: true }).click();
      const cell = editor.locator(`.sg-cell[data-addr="${addr}"]`); await expect(cell).toBeVisible({ timeout: 15_000 }); await cell.dblclick();
      const t = Date.now(); await editor.locator('.vs-editor').fill(text); await editor.locator('.vs-editor').press('Enter');
      await expect(cell.locator('.cell-text')).toHaveText(text, { timeout: 10_000 }); return Date.now() - t;
    };
    const e1 = await editAt('D10', 'edited near'); await expect(page.locator('#edit-state')).toHaveText('Unsaved changes');
    const e2 = await editAt(`D${last - 5}`, 'edited far'); m.edit_response_ms = Math.max(e1, e2);
    await expect(page.locator('#edit-state')).toHaveText('Unsaved changes');
    const t3 = Date.now(); await page.getByRole('button', { name: 'Save as next version' }).click();
    await expect(page.locator('.toast')).toContainText('saved inside', { timeout: 120_000 }); m.save_s = (Date.now() - t3) / 1000;
    await expect(page.locator('.file-meta')).toContainText('Version 2 of 2'); expect(m.save_s).toBeLessThan(30);

    // COMPLETE comparison: both far edits found, each navigable
    const t4 = Date.now(); await page.getByRole('button', { name: /Compare with v1/ }).click();
    await expect(page.getByTestId('completeness')).toContainText('COMPLETE', { timeout: 180_000 }); m.compare_s = (Date.now() - t4) / 1000;
    await expect(page.getByTestId('change-summary')).toContainText('2 modified');
    await expect(page.locator('.change-row')).toHaveCount(2); await expect(page.getByTestId('completeness')).toContainText('cells examined');
    await page.locator('.change-row').nth(1).click();
    await expect(page.getByTestId('sheet-target').locator('.sg-active')).toHaveCount(1, { timeout: 20_000 });
    await expect(page.getByTestId('sheet-target').locator(`.sg-cell.sg-active[data-addr="D${last - 5}"]`)).toBeVisible();
    await expect(page.getByTestId('sheet-target').locator('.sg-mark-mod')).toHaveCount(1);
    m.js_heap_mb = await page.evaluate(() => Math.round((performance.memory?.usedJSHeapSize ?? 0) / 1048576));
    let all = {}; try { all = JSON.parse(fs.readFileSync(results, 'utf8')); } catch { /* first */ }
    all[String(rows * 30)] = m; fs.writeFileSync(results, JSON.stringify(all, null, 1));
  });
}
