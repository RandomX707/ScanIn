import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

// Repeatability harness for the manager-ui DOCX scenario (upload -> open -> Edits -> save). Run with --repeat-each=N.
// Every /api request is timed; the slowest ones are written next to the private results so a stall is attributable.
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const sample = path.join(root, 'packages/api/samples/Employee_Onboarding_Guide.docx');
const out = path.join(root, '.private/feature-results/repeat-docx-upload');
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));

test.beforeEach(async ({ page }) => {
  await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token);
});

test('DOCX upload -> workspace appears promptly -> edit saves', async ({ page }, info) => {
  fs.mkdirSync(out, { recursive: true });
  const calls = [];
  const started = new Map();
  page.on('request', (r) => { if (r.url().includes('/api/')) started.set(r, Date.now()); });
  page.on('requestfinished', (r) => { const t = started.get(r); if (t) calls.push({ method: r.method(), path: new URL(r.url()).pathname.replace(/[0-9a-f-]{36}/g, ':id'), ms: Date.now() - t }); });
  page.on('requestfailed', (r) => { const t = started.get(r); if (t) calls.push({ method: r.method(), path: new URL(r.url()).pathname.replace(/[0-9a-f-]{36}/g, ':id'), ms: Date.now() - t, failed: r.failure()?.errorText }); });
  const name = `repeat-${crypto.randomBytes(4).toString('hex')}.docx`;
  const t0 = Date.now();
  try {
    await page.goto('/');
    await page.setInputFiles('#file-input', { name, mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer: fs.readFileSync(sample) });
    await expect(page.locator('.file-main h2')).toHaveText(name, { timeout: 60_000 });
    const shown = Date.now() - t0;
    await page.locator('.tab', { hasText: 'Edits' }).click();
    const target = page.locator('[data-target]').first();
    await target.click(); await page.keyboard.press('End'); await page.keyboard.type(' (revised)');
    await page.getByRole('button', { name: 'Save as next version' }).click();
    await expect(page.locator('.toast')).toContainText('saved inside', { timeout: 120_000 });
    fs.appendFileSync(path.join(out, 'runs.jsonl'), JSON.stringify({ ok: true, shown_ms: shown, total_ms: Date.now() - t0, slowest: calls.sort((a, b) => b.ms - a.ms).slice(0, 4) }) + '\n');
  } catch (e) {
    fs.appendFileSync(path.join(out, 'runs.jsonl'), JSON.stringify({ ok: false, error: String(e.message).split('\n')[0], total_ms: Date.now() - t0, calls: calls.sort((a, b) => b.ms - a.ms).slice(0, 8) }) + '\n');
    throw e;
  }
});
