import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

// Real stakeholder PDF (private, gitignored): skipped, never faked, when absent. Screenshots stay under .private/.
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const file = path.join(root, 'Policy Copies/Fire Policy copy.pdf');
const evidence = path.join(root, '.private/visual-evidence/pdf-fit');
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));
const API = process.env.IDW_API_BASE_URL || 'http://127.0.0.1:8010';
const auth = () => ({ Authorization: `Bearer ${run().token}` });
const AMOUNT = /^\d{1,2}(,\d\d)+,\d{3}$/;

test.beforeEach(async ({ page }) => { await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token); });

async function open(page) {
  const previewResponse = page.waitForResponse((r) => /\/versions\/[^/]+\/preview$/.test(r.url()) && r.status() === 200, { timeout: 120_000 });
  await page.goto('/');
  await page.setInputFiles('#file-input', file);
  await expect(page.locator('.file-main h2')).toHaveText(/Fire Policy copy/, { timeout: 90_000 });
  const [, documentId, versionId] = (await previewResponse).url().match(/documents\/([^/]+)\/versions\/([^/]+)\/preview/);
  return { documentId, versionId };
}

test.describe('PDF: longer replacement values are fitted safely or refused', () => {
  test.skip(!fs.existsSync(file), 'private stakeholder corpus not present');
  test('a longer amount fits naturally, a tight one offers fitting options, an absurd one is refused; the original is never changed', async ({ page }) => {
    test.setTimeout(420_000);
    fs.mkdirSync(evidence, { recursive: true });
    const { documentId, versionId } = await open(page);
    const sha = crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
    const preview = await (await fetch(`${API}/api/documents/${documentId}/versions/${versionId}/preview`, { headers: auth() })).json();
    test.skip(preview.edit.mode !== 'pdf_text', 'this PDF is not text-editable in this configuration');

    // find amount runs on page 2 and classify them with the fit endpoint (read-only)
    const runs = [];
    for (const pg of [2, 3, 4, 5, 6]) runs.push(...(await (await fetch(`${API}/api/documents/${documentId}/versions/${versionId}/pdf-text?page=${pg}`, { headers: auth() })).json()).runs.filter((r) => r.e && AMOUNT.test(r.text.trim())));
    expect(runs.length).toBeGreaterThan(2);
    const probe = async (r, suffix) => (await (await fetch(`${API}/api/documents/${documentId}/versions/${versionId}/pdf-edit/fit`, { method: 'POST', headers: { ...auth(), 'Content-Type': 'application/json' }, body: JSON.stringify({ pdf_edits: [{ id: r.id, old: r.text, new: r.text + suffix }] }) })).json()).fits[0];
    let natural = null, tight = null;
    let tightSuffix = '';
    for (const r of runs) {
      const f = await probe(r, ',678'); if (f.fits && !natural) natural = r;
      // the first number of extra digits that no longer fits naturally but still has a safe fitting option
      for (let n = 1; n <= 14 && !tight; n++) { const g = await probe(r, '6'.repeat(n)); if (!g.fits) { if (g.options.length) { tight = r; tightSuffix = '6'.repeat(n); } break; } }
    }
    expect(natural).toBeTruthy();
    expect(tight, 'the real policy has an amount that needs a fitting option').toBeTruthy();

    await page.locator('.tab', { hasText: 'Edits' }).click();
    await expect(page.locator('.pdf-run').first()).toBeVisible({ timeout: 60_000 });
    await page.locator('.pdf-page').nth(1).scrollIntoViewIfNeeded();
    await page.waitForTimeout(3000);
    const edit = async (r, value) => { await page.locator(`[data-run="${r.id}"]`).scrollIntoViewIfNeeded(); await page.locator(`[data-run="${r.id}"]`).click(); const input = page.locator(`input[data-run="${r.id}"]`); await input.fill(value); await input.press('Enter'); };

    // 1. the longer value fits naturally: previewed, warned about dependent totals, no fitting needed
    await edit(natural, natural.text + ',678');
    await expect(page.getByTestId('amount-warning')).toBeVisible({ timeout: 90_000 });
    await expect(page.getByTestId('fit-panel')).toHaveCount(0);
    await page.screenshot({ path: path.join(evidence, 'A-natural-fit.png') });

    // 2. a tight one: the safe options are offered, the fitted result is previewed, and can be reverted
    {
      await edit(tight, tight.text + tightSuffix);
      await expect(page.getByTestId('fit-panel')).toBeVisible({ timeout: 90_000 });
      await page.screenshot({ path: path.join(evidence, 'B-fit-options.png') });
      await page.getByTestId('fit-preview').click();
      await expect(page.getByTestId('fit-applied')).toBeVisible({ timeout: 90_000 });
      await page.screenshot({ path: path.join(evidence, 'C-fitted-preview.png') });
      await page.getByTestId('fit-applied').getByRole('button', { name: 'Revert to original value' }).click();
      await expect(page.getByTestId('fit-applied')).toHaveCount(0, { timeout: 90_000 });
      await edit(tight, tight.text + tightSuffix);
      await expect(page.getByTestId('fit-panel')).toBeVisible({ timeout: 90_000 });
      await page.getByTestId('fit-preview').click();
      await expect(page.getByTestId('fit-applied')).toBeVisible({ timeout: 90_000 });
    }

    // 3. an absurdly long value has no safe fit: refused with a reason, no fitting panel, nothing applied for it
    const other = runs.find((r) => r.id !== natural.id && r.id !== tight?.id) ?? natural;
    if (other.id !== natural.id) {
      await edit(other, other.text + ',111,222,333,444,555');
      await expect(page.getByTestId('fit-panel')).toHaveCount(0);
      await expect(page.locator(`[data-run="${other.id}"]`)).toHaveClass(/refused/, { timeout: 90_000 });
    }

    // save: new native PDF version containing both edits; original unchanged
    await page.getByRole('button', { name: 'Save as next version' }).click();
    await expect(page.locator('.toast, [role="status"]').filter({ hasText: /Version v2 saved/ }).first()).toBeVisible({ timeout: 180_000 });
    const history = await (await fetch(`${API}/api/documents/${documentId}/history`, { headers: auth() })).json();
    expect(history.versions.length).toBe(2);
    expect(history.versions[0].sha256).toBe(sha);
    expect(history.versions[1].sha256).not.toBe(sha);
    const derived = Buffer.from(await (await fetch(`${API}/api/documents/${documentId}/versions/${history.versions[1].version_id}/file`, { headers: auth() })).arrayBuffer());
    expect(derived.subarray(0, 5).toString()).toBe('%PDF-');
    fs.writeFileSync(path.join(evidence, 'derived.pdf'), derived);
    const check = await (await fetch(`${API}/api/documents/${documentId}/versions/${versionId}/pdf-text?page=2`, { headers: auth() })).json();
    expect(check.runs.some((r) => r.text === natural.text)).toBe(true);                     // the original still has the old value
    const cmp = await fetch(`${API}/api/documents/${documentId}/compare?base=${history.versions[0].version_id}&target=${history.versions[1].version_id}&wait=60`, { headers: auth() });
    expect(cmp.status).toBe(200);
    expect((await cmp.json()).summary.total).toBeGreaterThan(0);                              // the comparison identifies the edit
  });
});
