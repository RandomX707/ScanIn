import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Source-linked understanding on every private stakeholder document. Skipped (never faked) without the corpus.
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const manifestPath = path.join(root, '.private/inventory/manifest.json');
const corpus = path.join(root, 'Policy Copies');
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));
const manifest = fs.existsSync(manifestPath) ? JSON.parse(fs.readFileSync(manifestPath, 'utf8')) : null;
const seen = new Set();
const docs = manifest ? manifest.occurrences.filter((o) => !seen.has(o.sha256) && seen.add(o.sha256)) : [];
const file = (o) => (o.source === 'standalone' ? path.join(corpus, o.path) : path.join(root, '.private/corpus', o.source.replace(/\.zip$/, ''), o.path));

test.beforeEach(async ({ page }) => {
  await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token);
});

// Without the private evidence this file would register ZERO tests and vanish from the report. Say so explicitly instead (SKIPPED, with the prerequisite).
if (!manifest || docs.length !== 17) {
  test('PREREQUISITE (private stakeholder corpus): Source-linked chunks on the 17 stakeholder documents', () => {
    test.skip(true, 'private stakeholder corpus/manifest not present (.private/ and Policy Copies/ are not part of the repository); these acceptance checks did NOT run');
  });
}

test.describe('Chunks: meaningful, source-linked sections for all 17 stakeholder documents', () => {
  test.skip(!manifest || docs.length !== 17, 'private stakeholder corpus not present');
  for (const o of docs) {
    const name = path.basename(o.path);
    test(name, async ({ page }) => {
      test.skip(!fs.existsSync(file(o)), 'fixture missing');
      await page.goto('/');
      await page.setInputFiles('#file-input', file(o));
      await expect(page.locator('.file-main h2')).toHaveText(name, { timeout: 120_000 });
      await page.locator('.tab', { hasText: 'Chunks' }).click();
      const pill = page.locator('.section-title .pill').first();
      await expect(pill).toContainText(/\d+ sections?/, { timeout: 90_000 });
      const n = Number((await pill.textContent()).match(/(\d+)/)[1]);
      expect(n).toBeGreaterThanOrEqual(1);
      expect(n).toBeLessThanOrEqual(12);                                   // not dozens of arbitrary fragments
      await expect(page.locator('.source-link')).toContainText('Source:');  // every section says where it came from
      await expect(page.locator('.takeaway').first()).toBeVisible();
      if (o.kind === 'pdf') {
        await expect(page.locator('.source-link')).toContainText(/page/i);
        await expect(page.getByRole('button', { name: /View on page \d+/ })).toBeVisible();
      }
      if (o.kind === 'xlsx') await expect(page.locator('.source-link')).toContainText(/· rows? \d+/);   // worksheet name + row range
      // stepping through works and every section keeps its source
      for (let i = 1; i < n; i++) {
        await page.getByRole('button', { name: /Next chunk/ }).click();
        await expect(page.locator('.source-link')).toContainText('Source:');
      }
    });
  }
});
