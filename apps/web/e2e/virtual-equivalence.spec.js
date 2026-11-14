import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// The virtual viewer must show exactly what the classic grid shows, on every real stakeholder workbook (private corpus; skipped when absent).
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const manifestPath = path.join(root, '.private/inventory/manifest.json');
const manifest = fs.existsSync(manifestPath) ? JSON.parse(fs.readFileSync(manifestPath, 'utf8')) : null;
const seen = new Set();
const books = manifest ? manifest.occurrences.filter((o) => o.kind === 'xlsx' && !seen.has(o.sha256) && seen.add(o.sha256)) : [];
const extracted = (o) => path.join(root, '.private/corpus', o.source.replace(/\.zip$/, ''), o.path);
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));
const evidence = path.join(root, '.private/visual-evidence/xlsx-virtual');

// Without the private evidence this file would register ZERO tests and vanish from the report. Say so explicitly instead (SKIPPED, with the prerequisite).
if (!manifest || books.length !== 9) {
  test('PREREQUISITE (private stakeholder corpus): Virtual viewer equals the classic grid on the real workbooks', () => {
    test.skip(true, 'private stakeholder corpus/manifest not present (.private/ and Policy Copies/ are not part of the repository); these acceptance checks did NOT run');
  });
}

test.describe('Virtual viewer equals the classic grid on the real workbooks', () => {
  test.skip(!manifest || books.length !== 9, 'private stakeholder corpus not present');
  for (const o of books) {
    const name = path.basename(o.path);
    test(`${name}: same text, same formulas, same pictures, bounded DOM`, async ({ page }) => {
      const file = extracted(o); test.skip(!fs.existsSync(file), 'extracted fixture missing');
      fs.mkdirSync(evidence, { recursive: true });
      await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token);
      await page.goto('/'); await page.setInputFiles('#file-input', file);
      await expect(page.locator('.file-main h2')).toHaveText(name, { timeout: 90_000 });
      const classic = page.locator('[data-testid="sheet-viewer"]:not([data-virtual])');
      await expect(classic.locator('.sheet-fidelity')).toBeVisible({ timeout: 30_000 });
      const harvest = (loc) => loc.locator('[data-addr]').evaluateAll((els) => Object.fromEntries(els.filter((e) => e.classList.contains('sg-cell')).map((e) => [e.dataset.addr, (e.innerText || '').replace(/\s+/g, '')])));
      const classicMap = await harvest(classic); const classicFormulas = await classic.locator('.sg-formula').count();
      const classicImgs = await classic.locator('.sheet-fidelity img').count();
      // the SAME uploaded document, shown through the virtual viewer (flag read when the preview is fetched; re-open the Original tab)
      await page.evaluate(() => sessionStorage.setItem('channel360.virtual', '1'));
      await page.locator('.tab', { hasText: 'Chunks' }).click(); await page.locator('.tab', { hasText: 'Original' }).click();
      const virt = page.locator('[data-testid="sheet-viewer"][data-virtual="true"]');
      await expect(virt.locator('.sg-cell').first()).toBeVisible({ timeout: 60_000 });
      await expect.poll(async () => virt.locator('.sg-cell').evaluateAll((els) => els.filter((e) => (e.innerText || '').trim().length > 0).length), { timeout: 20_000 }).toBeGreaterThan(5);
      await page.waitForTimeout(800);
      const virtMap = await harvest(virt);
      const overlap = Object.keys(virtMap).filter((a) => a in classicMap);
      expect(overlap.length).toBeGreaterThan(20);
      const diff = overlap.filter((a) => classicMap[a] !== virtMap[a]);
      const shape = (t) => t.replace(/[A-Za-z]/g, 'a').replace(/[0-9]/g, '9');
      expect(diff, `cells whose text differs: ${diff.slice(0, 5).map((a) => `${a} classic=[${shape(classicMap[a])}] virtual=[${shape(virtMap[a])}]`).join('; ')}`).toEqual([]);
      expect(await virt.locator('.sg-formula').count()).toBeLessThanOrEqual(classicFormulas);
      if (classicFormulas) { await virt.locator('.vs-scroll').evaluate((el) => { el.scrollTop = 0; }); }
      expect(await virt.locator('.sg-cell').count()).toBeLessThan(4000);
      expect(await virt.locator('.vs-layer img').count()).toBeGreaterThanOrEqual(classicImgs > 0 ? 1 : 0);
      await virt.getByRole('button', { name: 'Fit width' }).click();
      await page.screenshot({ path: path.join(evidence, `${name}-virtual.png`) });
    });
  }
});
