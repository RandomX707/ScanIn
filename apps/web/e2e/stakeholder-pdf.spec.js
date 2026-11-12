import { test, expect } from '@playwright/test';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Private acceptance fixtures (gitignored). Without them this whole file is skipped, never faked.
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const corpus = process.env.CHANNEL360_CORPUS_DIR || path.join(root, 'Policy Copies');
const python = path.join(root, '.venv/bin/python');
const have = (name) => fs.existsSync(path.join(corpus, name));
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');
const scratch = path.join(root, '.private/e2e-output');

test.beforeEach(async ({ page }) => {
  await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token);
});
test.beforeAll(() => fs.mkdirSync(scratch, { recursive: true }));

async function upload(page, file) {
  await page.goto('/');
  await page.setInputFiles('#file-input', path.join(corpus, file));
  await expect(page.locator('.file-main h2')).toHaveText(file, { timeout: 90_000 });
}
async function download(page, trigger) {
  const [file] = await Promise.all([page.waitForEvent('download'), trigger()]);
  return { bytes: fs.readFileSync(await file.path()), name: file.suggestedFilename() };
}

test.describe('Real policy PDF (no form fields): genuine text editing', () => {
  test.skip(!have('CAR Policy copy.pdf'), 'private stakeholder corpus not present');

  test('upload, edit existing text, preview, save v2, download, old version untouched', async ({ page }) => {
    const file = 'CAR Policy copy.pdf';
    const original = fs.readFileSync(path.join(corpus, file));
    await upload(page, file);
    // Original shows the real document; Chunks has sections
    await page.locator('.tab', { hasText: 'Chunks' }).click();
    await expect(page.locator('.chunk-pagination .stage-note')).toBeVisible();
    await page.locator('.tab', { hasText: 'Edits' }).click();
    await expect(page.getByText('no form fields')).toHaveCount(0);
    const runs = page.locator('.pdf-run:not([data-locked])');
    await expect(runs.first()).toBeVisible({ timeout: 60_000 });
    // pick a real line of text
    const labels = await runs.evaluateAll((els) => els.map((el) => ({ id: el.dataset.run, label: el.getAttribute('aria-label') })));
    const pick = labels.find((l) => /^Edit text: .{14,36}$/.test(l.label) && l.label.includes(' '));
    expect(pick, 'a plain editable text line exists').toBeTruthy();
    const oldText = pick.label.replace('Edit text: ', '');
    const newText = oldText.split(' ').slice(0, -1).join(' ');
    await page.locator(`[data-run="${pick.id}"]`).click();
    const input = page.locator('.pdf-run-input');
    await input.fill(newText);
    await input.press('Enter');
    await expect(page.locator(`.pdf-run.changed[data-run="${pick.id}"]`)).toBeVisible({ timeout: 60_000 });
    await expect(page.locator('#edit-state')).toHaveText('Unsaved changes');
    await page.getByRole('button', { name: 'Save as next version' }).click();
    await expect(page.locator('.toast')).toContainText('saved inside', { timeout: 120_000 });
    await expect(page.locator('.file-meta')).toContainText('Version 2 of 2');
    // v2 is a real PDF with the new wording; unrelated pages identical (checked by an independent parser)
    const v2 = await download(page, () => page.getByRole('button', { name: /^Export/ }).click());
    expect(path.extname(v2.name)).toBe('.pdf');
    const out = path.join(scratch, 'car-v2.pdf');
    fs.writeFileSync(out, v2.bytes);
    expect(execFileSync(python, [path.join(root, 'scripts/corpus/verify_pdf_text.py'), path.join(corpus, file), out, oldText, newText]).toString().trim()).toBe('OK');
    // v1 is still byte-identical
    await page.getByRole('button', { name: /All versions/ }).click();
    await expect(page.locator('.version-item')).toHaveCount(2);
    await page.getByRole('button', { name: /Open v2/ }).click();
    await page.locator('.version-pill', { hasText: 'v1' }).click();
    const v1 = await download(page, () => page.locator('.reader-toolbar').getByRole('button', { name: 'Download exact source' }).click());
    expect(sha(v1.bytes)).toBe(sha(original));
  });

  test('a character the font lacks is refused with a plain sentence and nothing is saved', async ({ page }) => {
    await upload(page, 'CAR Policy copy.pdf');
    await page.locator('.tab', { hasText: 'Edits' }).click();
    const first = page.locator('.pdf-run:not([data-locked])').first();
    await expect(first).toBeVisible({ timeout: 60_000 });
    await first.click();
    const input = page.locator('.pdf-run-input');
    await input.fill((await input.inputValue()) + ' ₹₹');
    await input.press('Enter');
    await expect(page.locator('.toast.error')).toContainText("doesn't include one of the new characters", { timeout: 60_000 });
    await expect(page.locator('#edit-state')).toHaveText('No changes yet');
  });
});

test.describe('Signed policy PDFs stay read-only with a specific reason', () => {
  for (const [file, phrase] of [['Health Policy copy.pdf', 'digitally signed'], ['Motor  Policy.pdf', 'digitally signed'], ['Marine Policy copy.pdf', "doesn't match the file"]]) {
    test(file, async ({ page }) => {
      test.skip(!have(file), 'private stakeholder corpus not present');
      await upload(page, file);
      await page.locator('.tab', { hasText: 'Edits' }).click();
      await expect(page.locator('.empty h3')).toContainText(phrase);
      await expect(page.getByRole('button', { name: 'Save as next version' })).toHaveCount(0);
    });
  }
});
