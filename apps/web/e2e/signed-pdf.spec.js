import { test, expect } from '@playwright/test';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Real signed stakeholder policies (private, gitignored). Skipped, never faked, when absent.
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const corpus = process.env.CHANNEL360_CORPUS_DIR || path.join(root, 'Policy Copies');
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));
const health = path.join(corpus, 'Health Policy copy.pdf');
const marine = path.join(corpus, 'Marine Policy copy.pdf');
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');

test.beforeEach(async ({ page }) => {
  await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token);
});
async function upload(page, file) {
  await page.goto('/');
  await page.setInputFiles('#file-input', file);
  await expect(page.locator('.file-main h2')).toHaveText(path.basename(file), { timeout: 90_000 });
}

test.describe('Digitally signed PDFs', () => {
  test.skip(!fs.existsSync(health) || !fs.existsSync(marine), 'private stakeholder corpus not present');

  test('Health: truthful status, blocked original, acknowledged working copy, edit, export, compare, original untouched', async ({ page }) => {
    await upload(page, health);
    // Original tab states the signature truthfully: intact, but the signer is NOT trusted without a trust policy
    const status = page.getByTestId('signature-status');
    await expect(status).toBeVisible({ timeout: 30_000 });
    await expect(status).toHaveAttribute('data-state', 'integrity_valid_trust_not_established');
    await expect(status).toContainText('signer not trusted');
    await expect(status).toContainText('Not validated as trusted');
    await expect(status).toContainText('never claims legal acceptance');

    await page.locator('.tab', { hasText: 'Edits' }).click();
    await expect(page.locator('.pdf-run')).toHaveCount(0);                     // the signed original has no editor
    await expect(page.locator('.empty h3')).toContainText('digitally signed');
    // working copy needs an explicit acknowledgement
    await page.getByTestId('create-working-copy').click();
    await expect(page.getByRole('dialog')).toContainText('will NOT carry the original signer');
    const confirm = page.getByTestId('confirm-working-copy');
    await expect(confirm).toBeDisabled();
    await page.getByRole('dialog').getByRole('checkbox').check();
    await expect(confirm).toBeEnabled();
    await confirm.click();
    // now on the unsigned working copy: persistent notice, editor, no signature
    await expect(page.locator('.file-meta')).toContainText('Version 2 of 2', { timeout: 120_000 });
    await expect(page.locator('.file-meta')).toContainText('Unsigned working copy · not official');
    await expect(page.getByTestId('working-copy-banner').first()).toContainText('DRAFT — MODIFIED COPY — NOT OFFICIAL');
    await expect(page.getByTestId('working-copy-banner').first()).toContainText('not issued or approved');
    const runs = page.locator('.pdf-run:not([data-locked])');
    await expect(runs.first()).toBeVisible({ timeout: 60_000 });
    const labels = await runs.evaluateAll((els) => els.map((el) => ({ id: el.dataset.run, label: el.getAttribute('aria-label') })));
    expect(labels.some((l) => /NOT OFFICIAL/.test(l.label))).toBe(false);      // the notice strip is not editable
    const pick = labels.find((l) => /^Edit text: .{14,36}$/.test(l.label) && l.label.replace('Edit text: ', '').includes(' '));
    expect(pick, 'an editable line exists on the working copy').toBeTruthy();
    const oldText = pick.label.replace('Edit text: ', '');
    await page.locator(`[data-run="${pick.id}"]`).click();
    const input = page.locator('.pdf-run-input');
    await input.fill(oldText.split(' ').slice(0, -1).join(' '));
    await input.press('Enter');
    await expect(page.locator(`.pdf-run.changed[data-run="${pick.id}"]`)).toBeVisible({ timeout: 60_000 });
    await page.getByRole('button', { name: 'Save as next version' }).click();
    await expect(page.locator('.toast')).toContainText('saved inside', { timeout: 120_000 });
    await expect(page.locator('.file-meta')).toContainText('Version 3 of 3');
    await expect(page.getByTestId('working-copy-banner').first()).toBeVisible();
    // export: a genuine PDF with no signature structures and the marker
    const [dl] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: /^Export/ }).click()]);
    const bytes = fs.readFileSync(await dl.path());
    expect(bytes.subarray(0, 5).toString()).toBe('%PDF-');
    expect(bytes.includes('/ByteRange')).toBe(false);
    // compare the edited working copy with the signed original
    await page.getByRole('button', { name: /All versions/ }).click();
    await expect(page.locator('.version-item')).toHaveCount(3);
    await expect(page.locator('.version-item').nth(1)).toContainText('Unsigned working copy · not official');
    await page.locator('.version-item').filter({ hasText: 'v3' }).getByLabel('Compare v3 with another version').selectOption({ label: 'v1 · Original' });
    await expect(page.getByTestId('compare-page')).toBeVisible();
    await expect(page.getByTestId('relation')).toContainText('descends from v1 through v1 → v2 → v3');
    await expect(page.locator('.audit-lines')).toContainText('Unsigned working copy · not official');
    await expect(page.locator('.audit-lines')).toContainText('Signature structure present · not validated');
    // signed original is byte-identical
    await page.getByRole('button', { name: 'Open v1' }).click();
    const [d1] = await Promise.all([page.waitForEvent('download'), page.locator('.reader-toolbar').getByRole('button', { name: 'Download exact source' }).click()]);
    expect(sha(fs.readFileSync(await d1.path()))).toBe(sha(fs.readFileSync(health)));
  });

  test('Marine: a malformed signature container is reported as malformed, never as trusted', async ({ page }) => {
    await upload(page, marine);
    const status = page.getByTestId('signature-status');
    await expect(status).toBeVisible({ timeout: 30_000 });
    await expect(status).toHaveAttribute('data-state', 'malformed');
    await expect(status).toContainText('Malformed signature · not trusted');
    await expect(status).not.toContainText('trusted by policy');
    await page.locator('.tab', { hasText: 'Edits' }).click();
    await expect(page.getByTestId('create-working-copy')).toBeVisible();
    await expect(page.locator('.pdf-run')).toHaveCount(0);
  });
});
