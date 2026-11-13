import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Real stakeholder DOCX (private, gitignored); skipped, never faked, when absent. Evidence screenshots stay under .private/.
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const file = path.join(root, 'Policy Copies/QCR/Health Quote Format.docx');
const evidence = path.join(root, '.private/visual-evidence/docx');
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));

test.beforeEach(async ({ page }) => {
  await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token);
});

test.describe('Health Quote Format.docx is shown as real pages', () => {
  test.skip(!fs.existsSync(file), 'private stakeholder corpus not present');
  test('pages, logo, illustration, links, zoom, full screen and chunk-to-page link', async ({ page }) => {
    fs.mkdirSync(evidence, { recursive: true });
    await page.goto('/');
    await page.setInputFiles('#file-input', file);
    await expect(page.locator('.file-main h2')).toHaveText('Health Quote Format.docx', { timeout: 90_000 });
    const viewer = page.getByTestId('docx-page-viewer');
    await expect(viewer).toBeVisible({ timeout: 60_000 });
    await expect(page.locator('.extracted-banner')).toHaveCount(0);                        // not the extracted-text fallback
    await expect(viewer.locator('.viewer-page')).toHaveCount(4);
    await expect(viewer.locator('canvas').first()).toBeVisible();
    await expect(page.locator('.file-meta')).toContainText('Page preview');
    // the first page really painted: logo block in the header band and the illustration below it (not a blank white page)
    await page.waitForTimeout(1500);
    const paint = await viewer.locator('canvas').first().evaluate((c) => {
      const ctx = c.getContext('2d'); const w = c.width, h = c.height;
      const frac = (y0, y1) => { const d = ctx.getImageData(0, Math.floor(h * y0), w, Math.floor(h * (y1 - y0))).data; let n = 0; for (let i = 0; i < d.length; i += 4) if (d[i] < 235 || d[i + 1] < 235 || d[i + 2] < 235) n++; return n / (d.length / 4); };
      return { header: frac(0, 0.2), illustration: frac(0.22, 0.45), corner: Array.from(ctx.getImageData(5, 5, 1, 1).data) };
    });
    expect(paint.header).toBeGreaterThan(0.01);
    expect(paint.illustration).toBeGreaterThan(0.02);
    expect(paint.corner[0]).toBeGreaterThan(245);                                         // the file defines no dark page background
    await page.screenshot({ path: path.join(evidence, 'browser-original-tab.png') });

    // page navigation and zoom
    await viewer.getByRole('button', { name: 'Next page' }).click();
    await expect(viewer.locator('.vt-page')).toContainText('Page 2 of 4');
    await viewer.getByRole('button', { name: 'Actual size' }).click();
    const w100 = (await viewer.locator('.viewer-page').first().boundingBox()).width;
    await viewer.getByRole('button', { name: 'Zoom in' }).click();
    expect((await viewer.locator('.viewer-page').first().boundingBox()).width).toBeGreaterThan(w100 * 1.2);
    await viewer.getByRole('button', { name: 'Fit width' }).click();

    // full screen keeps the same document and page
    await viewer.getByRole('button', { name: 'Full screen' }).click();
    await expect(page.locator('.page-viewer.expanded')).toBeVisible();
    const vp = page.viewportSize(); const box = await page.locator('.page-viewer.expanded').boundingBox();
    expect(box.width).toBeGreaterThanOrEqual(vp.width - 2);
    await page.screenshot({ path: path.join(evidence, 'browser-full-screen.png') });
    await page.keyboard.press('Escape');
    await expect(page.locator('.page-viewer.expanded')).toHaveCount(0);

    // Chunks: a section links to the page where it starts, and the viewer opens at that page
    await page.locator('.tab', { hasText: 'Chunks' }).click();
    const link = page.getByRole('button', { name: /View on page \d+/ });
    await expect(link).toBeVisible({ timeout: 30_000 });
    const target = Number((await link.innerText()).match(/page (\d+)/)[1]);
    await link.click();
    await expect(page.getByTestId('docx-page-viewer').locator('.vt-page')).toContainText(`Page ${target} of 4`, { timeout: 15_000 });

    // Edits still offer native DOCX editing
    await page.locator('.tab', { hasText: 'Edits' }).click();
    await expect(page.locator('.docx-editor [data-target]').first()).toBeVisible({ timeout: 30_000 });
  });
});
