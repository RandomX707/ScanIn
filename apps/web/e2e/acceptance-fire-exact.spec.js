import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

// Final acceptance: the exact replacement on the real Fire policy, end to end through the UI, in an isolated tenant. Artifacts stay under .private/.
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const file = path.join(root, 'Policy Copies/Fire Policy copy.pdf');
const out = path.join(root, '.private/acceptance');
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));
const API = process.env.IDW_API_BASE_URL || 'http://127.0.0.1:8010';
const H = () => ({ Authorization: `Bearer ${run().token}` });
const NEW = '11,11,11,111,678';   // synthetic replacement typed into a real field

test.beforeEach(async ({ page }) => { await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token); });

test('exact replacement on the Fire policy: fit, preview, save, reopen, compare', async ({ page }) => {
  test.skip(!fs.existsSync(file), 'private corpus not present');
  test.setTimeout(300_000); fs.mkdirSync(out, { recursive: true });
  const pr = page.waitForResponse((r) => /\/versions\/[^/]+\/preview$/.test(r.url()) && r.status() === 200, { timeout: 120_000 });
  await page.goto('/'); await page.setInputFiles('#file-input', file);
  await expect(page.locator('.file-main h2')).toHaveText(/Fire Policy copy/, { timeout: 90_000 });
  const [, d, v] = (await pr).url().match(/documents\/([^/]+)\/versions\/([^/]+)\/preview/);
  const original = fs.readFileSync(file); const sha = crypto.createHash('sha256').update(original).digest('hex');
  const runs = (await (await fetch(`${API}/api/documents/${d}/versions/${v}/pdf-text?page=2`, { headers: H() })).json()).runs;
  const target = runs.find((r) => r.e && /^\d{1,2}(,\d\d)+,\d{3}$/.test(r.text.trim()));   // the first thousand-separated sum-insured amount on page 2
  expect(target).toBeTruthy();
  const fit = await (await fetch(`${API}/api/documents/${d}/versions/${v}/pdf-edit/fit`, { method: 'POST', headers: { ...H(), 'Content-Type': 'application/json' }, body: JSON.stringify({ pdf_edits: [{ id: target.id, old: target.text, new: NEW }] }) })).json();
  fs.writeFileSync(path.join(out, 'fire-exact-fit-response.json'), JSON.stringify(fit, null, 1));
  expect(fit.fits[0].fits).toBe(true);
  await page.locator('.tab', { hasText: 'Edits' }).click();
  await page.locator(`[data-run="${target.id}"]`).scrollIntoViewIfNeeded(); await page.waitForTimeout(2500);
  await page.locator(`[data-run="${target.id}"]`).click();
  const input = page.locator(`input[data-run="${target.id}"]`); await input.fill(NEW); await input.press('Enter');
  await expect(page.getByTestId('amount-warning')).toBeVisible({ timeout: 90_000 });
  await expect(page.getByTestId('fit-panel')).toHaveCount(0);
  await expect(page.getByTestId('adjacent-text-note')).toContainText(`${NEW}.00`);   // the complete displayed value is stated, including the separate .00 object
  await page.getByRole('button', { name: 'Save as next version' }).click();
  await expect(page.locator('.toast, [role="status"]').filter({ hasText: /Version v2 saved/ }).first()).toBeVisible({ timeout: 180_000 });
  const hist = await (await fetch(`${API}/api/documents/${d}/history`, { headers: H() })).json();
  expect(hist.versions.length).toBe(2); expect(hist.versions[0].sha256).toBe(sha);
  const get = async (vid) => Buffer.from(await (await fetch(`${API}/api/documents/${d}/versions/${vid}/file`, { headers: H() })).arrayBuffer());
  const v1 = await get(hist.versions[0].version_id), v2 = await get(hist.versions[1].version_id);
  expect(crypto.createHash('sha256').update(v1).digest('hex')).toBe(sha);
  fs.writeFileSync(path.join(out, 'fire-v1.pdf'), v1); fs.writeFileSync(path.join(out, 'fire-v2.pdf'), v2);
  let cmp = {}; for (let i = 0; i < 12; i++) { const r = await fetch(`${API}/api/documents/${d}/compare?base=${hist.versions[0].version_id}&target=${hist.versions[1].version_id}&wait=20`, { headers: H() }); cmp = await r.json(); if (r.status === 200) break; }
  fs.writeFileSync(path.join(out, 'fire-compare-raw-keys.json'), JSON.stringify(Object.keys(cmp)));
  fs.writeFileSync(path.join(out, 'fire-compare.json'), JSON.stringify({ summary: cmp.summary, scan: cmp.scan_status ?? cmp.complete, changes: (cmp.changes ?? []).length, history: hist.versions.map((x) => ({ number: x.number, parent: x.parent_number ?? x.parent_version_number ?? null, kind: x.kind, classification: x.classification })) }, null, 1));
  expect(cmp.summary.total).toBeGreaterThan(0);
});
