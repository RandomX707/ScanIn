import { test, expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const python = path.join(root, '.venv/bin/python');
const corpus = process.env.CHANNEL360_CORPUS_DIR || path.join(root, 'Policy Copies');
const api = process.env.IDW_API_BASE_URL || 'http://127.0.0.1:8000';
const folderCount = async () => (await (await fetch(`${api}/api/folders`, { headers: { Authorization: `Bearer ${run().token}` } })).json()).items.length;
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));

test.beforeEach(async ({ page }) => {
  await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token);
});

test('uploading identical bytes under another name discloses the existing document', async ({ page }) => {
  const body = Buffer.from(`id,item\n1,dup-${Date.now()}\n`);
  await page.goto('/');
  await page.setInputFiles('#file-input', { name: 'e2e-dup-first.csv', mimeType: 'text/csv', buffer: body });
  await expect(page.locator('.file-main h2')).toHaveText('e2e-dup-first.csv', { timeout: 60_000 });
  await page.goto('/');
  await page.setInputFiles('#file-input', { name: 'e2e-dup-second.csv', mimeType: 'text/csv', buffer: body });
  await expect(page.locator('.toast')).toContainText('is identical to “e2e-dup-first.csv”', { timeout: 60_000 });
});

test.describe('ZIP archives are imported as their documents', () => {
  test('a hostile archive imports only the safe files and reports every refusal', async ({ page }) => {
    const out = path.join(root, '.runtime/browser-fixtures');
    fs.mkdirSync(out, { recursive: true });
    const zip = path.join(out, 'e2e-hostile.zip');
    execFileSync(python, [path.join(root, 'scripts/corpus/make_hostile_zip.py'), zip]);
    await page.goto('/');
    await page.setInputFiles('#file-input', zip);
    const toast = page.locator('.toast');
    await expect(toast).toContainText('e2e-hostile.zip: 2 documents found — 2 added', { timeout: 120_000 });
    await expect(toast).toContainText('Not imported');
    await expect(page.locator('.history-item', { hasText: 'ok-orders' })).toHaveCount(1);
    await expect(page.locator('.history-item', { hasText: 'ok-notes' })).toHaveCount(1);
    for (const bad of ['evil', 'link', 'inner', 'secret', 'bomb', 'run']) await expect(page.locator('.history-item', { hasText: new RegExp(`^${bad}\\b`) })).toHaveCount(0);
  });

  test('QCR.zip gives 7 documents and RFQ.zip 17 recognised entries (duplicates linked, not re-added)', async ({ page }) => {
    test.skip(!fs.existsSync(path.join(corpus, 'QCR.zip')) || !fs.existsSync(path.join(corpus, 'RFQ.zip')), 'private stakeholder corpus not present');
    const base = await folderCount();                                        // earlier tests in this run share the tenant
    await page.goto('/');
    await page.setInputFiles('#file-input', path.join(corpus, 'QCR.zip'));
    await expect(page.locator('.toast')).toContainText('QCR.zip: 7 documents found — 7 added', { timeout: 180_000 });
    await expect(page.locator('.history-item')).toHaveCount(base + 7);
    await page.goto('/');
    await page.setInputFiles('#file-input', path.join(corpus, 'RFQ.zip'));
    await expect(page.locator('.toast')).toContainText('RFQ.zip: 17 documents found — 10 added, 7 already in your workspace', { timeout: 180_000 });
    await expect(page.locator('.history-item')).toHaveCount(base + 17);
    await expect(page.locator('.history-item', { hasText: /Marine\s+RFQ/ })).toHaveCount(1);
    const names = (await (await fetch(`${api}/api/folders`, { headers: { Authorization: `Bearer ${run().token}` } })).json()).items.map((f) => f.name);
    expect(names).toContain('Marine\u00a0 RFQ.xlsx');                                                   // non-breaking space preserved exactly
    await expect(page.locator('.history-item', { hasText: 'GMC RFQ' })).toHaveCount(1);          // upper-case extension
  });
});
