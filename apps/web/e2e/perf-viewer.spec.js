import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Viewer performance on the 72-page Fire policy: lazy page rendering, scrolling through every page, zoom and full screen. Private corpus; skipped when absent.
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const fire = path.join(process.env.CHANNEL360_CORPUS_DIR || path.join(root, 'Policy Copies'), 'Fire Policy copy.pdf');
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));
test.beforeEach(async ({ page }) => { await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token); });

test('72-page PDF: compare viewer paints quickly, scrolls through every page, zooms and goes full screen without errors', async ({ page }) => {
  test.skip(!fs.existsSync(fire), 'private stakeholder corpus not present');
  const errors = []; page.on('pageerror', (e) => errors.push(String(e))); page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  await page.goto('/'); await page.setInputFiles('#file-input', fire);
  await expect(page.locator('.file-main h2')).toHaveText('Fire Policy copy.pdf', { timeout: 90_000 });
  await page.locator('.tab', { hasText: 'Edits' }).click();
  const runs = page.locator('.pdf-run:not([data-locked])'); await expect(runs.first()).toBeVisible({ timeout: 60_000 });
  const pick = (await runs.evaluateAll((els) => els.map((e) => ({ id: e.dataset.run, label: e.getAttribute('aria-label') })))).find((l) => /^Edit text: .{14,36}$/.test(l.label) && l.label.replace('Edit text: ', '').includes(' '));
  await page.locator(`[data-run="${pick.id}"]`).click(); const input = page.locator('.pdf-run-input'); await input.fill(pick.label.replace('Edit text: ', '').split(' ').slice(0, -1).join(' ')); await input.press('Enter');
  await expect(page.locator(`.pdf-run.changed[data-run="${pick.id}"]`)).toBeVisible({ timeout: 60_000 });
  await page.getByRole('button', { name: 'Save as next version' }).click(); await expect(page.locator('.file-meta')).toContainText('Version 2 of 2', { timeout: 120_000 });
  await page.getByRole('button', { name: /Compare with v1/ }).click();
  const t0 = Date.now();
  const viewer = page.getByTestId('viewer-target'); await expect(viewer.locator('.viewer-page')).toHaveCount(72, { timeout: 60_000 });
  await expect.poll(async () => viewer.locator('canvas').first().evaluate((c) => { const d = c.getContext('2d').getImageData(0, 0, c.width, Math.min(c.height, 200)).data; for (let i = 0; i < d.length; i += 4) if (d[i] < 200) return true; return false; }), { timeout: 30_000 }).toBe(true);
  const firstPaint = Date.now() - t0; expect(firstPaint).toBeLessThan(15_000);
  const scroller = viewer.locator('.viewer-scroll'); const t1 = Date.now();
  for (let step = 0; step < 40; step++) { await scroller.evaluate((el) => { el.scrollTop += el.clientHeight * 0.9; }); await page.waitForTimeout(60); }
  await scroller.evaluate((el) => { el.scrollTop = el.scrollHeight; }); await page.waitForTimeout(800);
  expect(Date.now() - t1).toBeLessThan(40_000);
  const lastPainted = await viewer.locator('.viewer-page').last().locator('canvas').evaluate((c) => { const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; for (let i = 0; i < d.length; i += 4) if (d[i] < 200) return true; return false; });
  expect(lastPainted).toBe(true);                                                               // page 72 was lazily rendered
  await viewer.getByRole('button', { name: 'Zoom in' }).click(); await viewer.getByRole('button', { name: 'Zoom out' }).click(); await viewer.getByRole('button', { name: 'Fit width' }).click();
  await viewer.getByRole('button', { name: 'Full screen' }).click(); await expect(page.locator('.page-viewer.expanded')).toBeVisible(); await page.keyboard.press('Escape');
  const painted = await viewer.locator('canvas').evaluateAll((cs) => cs.filter((c) => c.width > 0 && c.getContext('2d').getImageData(0, 0, 1, 1).data[3] > 0).length);
  fs.mkdirSync(path.join(root, '.private/feature-results'), { recursive: true });
  fs.writeFileSync(path.join(root, '.private/feature-results/viewer-perf.json'), JSON.stringify({ pages: 72, first_paint_ms: firstPaint, scroll_through_ms: Date.now() - t1, canvases_with_content: painted, console_errors: errors.length }));
  expect(errors.filter((e) => !/favicon|ResizeObserver/.test(e))).toEqual([]);
});
