import { test, expect } from '@playwright/test';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// The four features together, in a real browser, on the real stakeholder files (private, gitignored; skipped, never faked, when absent).
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const corpus = process.env.CHANNEL360_CORPUS_DIR || path.join(root, 'Policy Copies');
const python = (process.env.IDW_E2E_PYTHON || path.join(root, '.venv/bin/python'));
const api = process.env.IDW_API_BASE_URL || 'http://127.0.0.1:8010';
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));
const scratch = path.join(root, '.private/e2e-output'); const evidence = path.join(root, '.private/visual-evidence/together');
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');
const files = {
  car: path.join(corpus, 'CAR Policy copy.pdf'), motor: path.join(corpus, 'Motor  Policy.pdf'), docx: path.join(corpus, 'QCR/Health Quote Format.docx'),
  xlsx: path.join(corpus, 'QCR/GMC QCR Format.XLSX'),
};
const have = Object.values(files).every((f) => fs.existsSync(f));

test.beforeAll(() => { fs.mkdirSync(scratch, { recursive: true }); fs.mkdirSync(evidence, { recursive: true }); });
test.beforeEach(async ({ page }) => { await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token); });

const auth = () => ({ headers: { Authorization: `Bearer ${run().token}` } });
async function folderOf(page, name) {
  const items = (await (await page.request.get(`${api}/api/folders?q=${encodeURIComponent(name)}`, auth())).json()).items;
  return items.filter((f) => f.name === name).sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
}
async function bytesOf(page, folder, n) {
  const v = folder.versions[n - 1];
  return Buffer.from(await (await page.request.get(`${api}/api/documents/${folder.document_id}/versions/${v.version_id}/file`, auth())).body());
}
async function upload(page, file) {
  await page.goto('/');
  await page.setInputFiles('#file-input', file);
  await expect(page.locator('.file-main h2')).toHaveText(path.basename(file), { timeout: 90_000 });
}
async function saveNext(page, n) {
  await page.getByRole('button', { name: 'Save as next version' }).click();
  await expect(page.locator('.toast')).toContainText('saved inside', { timeout: 120_000 });
  await expect(page.locator('.file-meta')).toContainText(`Version ${n} of ${n}`);
}
async function exportFile(page) {
  const [dl] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: /^Export/ }).click()]);
  return { bytes: fs.readFileSync(await dl.path()), name: dl.suggestedFilename() };
}
const verify = (args) => execFileSync(python, args).toString().trim();

test.describe('Document experience: all four features, real browser, real files', () => {
  test.skip(!have, 'private stakeholder corpus not present');

  test('1 PDF: edit twice, compare any two versions, download v3 and reopen independently, v1 immutable, lineage', async ({ page }) => {
    const original = fs.readFileSync(files.car);
    await upload(page, files.car);
    await page.locator('.tab', { hasText: 'Edits' }).click();
    const runs = page.locator('.pdf-run:not([data-locked])');
    await expect(runs.first()).toBeVisible({ timeout: 60_000 });
    const multi = (l) => /^Edit text: .{14,36}$/.test(l.label) && l.label.replace('Edit text: ', '').includes(' ');
    const edit = async (pick) => {
      const oldText = pick.label.replace('Edit text: ', ''); const next = oldText.split(' ').slice(0, -1).join(' ');
      await page.locator(`[data-run="${pick.id}"]`).click(); const input = page.locator('.pdf-run-input'); await input.fill(next); await input.press('Enter');
      await expect(page.locator(`.pdf-run.changed[data-run="${pick.id}"]`)).toBeVisible({ timeout: 60_000 });
      return { oldText, next };
    };
    const first = (await runs.evaluateAll((els) => els.map((e) => ({ id: e.dataset.run, label: e.getAttribute('aria-label') })))).filter(multi);
    await edit(first[0]); await saveNext(page, 2);
    await page.locator('.tab', { hasText: 'Edits' }).click(); await expect(runs.first()).toBeVisible({ timeout: 60_000 });
    const labels2 = (await runs.evaluateAll((els) => els.map((e) => ({ id: e.dataset.run, label: e.getAttribute('aria-label') })))).filter(multi);
    const second = labels2.find((l) => l.label !== first[0].label.replace(/ [^ ]+$/, '') && l.label !== first[0].label) ?? labels2[1];
    const e2 = await edit(second); await saveNext(page, 3);
    // independent re-open of the exported v3
    const v3 = await exportFile(page); expect(path.extname(v3.name)).toBe('.pdf');
    const v2file = path.join(scratch, 'car-v2.pdf'), v3file = path.join(scratch, 'car-v3.pdf');
    const folder = await folderOf(page, 'CAR Policy copy.pdf');
    fs.writeFileSync(v2file, await bytesOf(page, folder, 2)); fs.writeFileSync(v3file, v3.bytes);
    expect(verify([path.join(root, 'scripts/corpus/verify_pdf_text.py'), v2file, v3file, e2.oldText, e2.next])).toBe('OK');
    // lineage + immutability
    expect(folder.versions.map((v) => v.parent_number)).toEqual([null, 1, 2]);
    expect(sha(await bytesOf(page, folder, 1))).toBe(sha(original));
    // compare any two (UI)
    await page.getByRole('button', { name: /All versions/ }).click();
    await page.locator('.version-item').filter({ hasText: 'v3' }).getByLabel('Compare v3 with another version').selectOption({ label: 'v1 · Original' });
    await expect(page.getByTestId('relation')).toContainText('v1 → v2 → v3');
    await expect(page.getByTestId('change-summary')).toContainText('2 modified');
    await expect(page.getByTestId('viewer-base').locator('.hl-mod').first()).toBeVisible({ timeout: 60_000 });
    await page.screenshot({ path: path.join(evidence, 'pdf-compare.png'), fullPage: true });
    await page.getByLabel('Base version').selectOption({ label: 'v2 · from v1' });
    await expect(page.getByTestId('change-summary')).toContainText('1 modified');
  });

  test('2 XLSX: expand and navigate, edit, download and reopen independently, compare marks, lineage, immutability', async ({ page }) => {
    const original = fs.readFileSync(files.xlsx);
    await upload(page, files.xlsx);
    const viewer = page.getByTestId('sheet-viewer'); await expect(viewer.locator('.sheet-fidelity')).toBeVisible({ timeout: 30_000 });
    await viewer.getByRole('button', { name: 'Full screen' }).click();
    await expect(page.locator('[data-testid="sheet-viewer"].expanded')).toBeVisible();
    await viewer.getByRole('button', { name: 'Fit width' }).click();
    await viewer.locator('td.sg-cell[data-addr]').nth(3).click(); const sel = await viewer.locator('.sg-selected').first().getAttribute('data-addr');
    await viewer.locator('.sheet-scroll').evaluate((el) => { el.scrollTop = 400; });
    expect(await viewer.locator('.sg-formula').count()).toBeGreaterThan(0);                // formulas are shown and marked
    expect(await viewer.locator('.sg-selected').first().getAttribute('data-addr')).toBe(sel);
    await page.screenshot({ path: path.join(evidence, 'xlsx-expanded.png') });
    await page.keyboard.press('Escape');
    await page.locator('.tab', { hasText: 'Edits' }).click();
    const editor = page.getByTestId('sheet-editor'); const inputs = editor.locator('.cell-input'); await expect(inputs.first()).toBeVisible();
    let cell = null, id = '';
    for (let i = 0; i < Math.min(await inputs.count(), 40); i++) { const v = await inputs.nth(i).inputValue(); if (v && /[A-Za-z]/.test(v) && v.length < 50) { cell = inputs.nth(i); id = await cell.getAttribute('data-cell'); break; } }
    expect(cell).not.toBeNull();
    await cell.fill((await cell.inputValue()) + ' e2e');
    await editor.getByRole('button', { name: 'Full screen' }).click(); await expect(page.locator('[data-testid="sheet-editor"].expanded')).toBeVisible();
    await page.keyboard.press('Escape'); await expect(page.locator('#edit-state')).toHaveText('Unsaved changes');
    await saveNext(page, 2);
    const out = await exportFile(page); expect(path.extname(out.name).toLowerCase()).toBe('.xlsx');
    const a = path.join(scratch, 'gmc-v1.xlsx'), b = path.join(scratch, 'gmc-v2.xlsx'); fs.writeFileSync(a, original); fs.writeFileSync(b, out.bytes);
    expect(verify([path.join(root, 'scripts/corpus/verify_office_edit.py'), 'xlsx', a, b, id.replace(/^xlsx:/, '')])).toBe('OK');
    const folder = await folderOf(page, 'GMC QCR Format.XLSX');
    expect(folder.versions.map((v) => v.parent_number)).toEqual([null, 1]); expect(sha(await bytesOf(page, folder, 1))).toBe(sha(original));
    await page.getByRole('button', { name: /Compare with v1/ }).click();
    await expect(page.getByTestId('change-summary')).toContainText('1 modified');
    await expect(page.getByTestId('sheet-target').locator('.sg-mark-mod')).toHaveCount(1, { timeout: 30_000 });
    await page.screenshot({ path: path.join(evidence, 'xlsx-compare.png'), fullPage: true });
  });

  test('3 DOCX: real page layout, edit, download and reopen independently, compare, lineage, immutability', async ({ page }) => {
    const original = fs.readFileSync(files.docx);
    await upload(page, files.docx);
    const viewer = page.getByTestId('docx-page-viewer'); await expect(viewer.locator('.viewer-page')).toHaveCount(4, { timeout: 60_000 });
    await page.waitForTimeout(1500);
    const painted = await viewer.locator('canvas').first().evaluate((c) => { const ctx = c.getContext('2d'); const d = ctx.getImageData(0, 0, c.width, Math.floor(c.height * 0.45)).data; let n = 0; for (let i = 0; i < d.length; i += 4) if (d[i] < 235 || d[i + 1] < 235 || d[i + 2] < 235) n++; return n / (d.length / 4); });
    expect(painted).toBeGreaterThan(0.03);                                                // logo + illustration actually drawn
    await page.screenshot({ path: path.join(evidence, 'docx-pages.png') });
    await page.locator('.tab', { hasText: 'Edits' }).click();
    const editable = page.locator('.docx-editor [data-target]'); await expect(editable.first()).toBeVisible({ timeout: 30_000 });
    const target = editable.nth(3); const oldText = await target.evaluate((el) => el.textContent);   // exact text, not whitespace-normalised
    await target.click();
    await target.evaluate((el) => { const r = document.createRange(); r.selectNodeContents(el); r.collapse(false); const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r); });   // caret at the true end (the line may wrap)
    await page.keyboard.type(' (reviewed)');
    await saveNext(page, 2);
    const out = await exportFile(page); expect(path.extname(out.name).toLowerCase()).toBe('.docx');
    const a = path.join(scratch, 'health-v1.docx'), b = path.join(scratch, 'health-v2.docx'); fs.writeFileSync(a, original); fs.writeFileSync(b, out.bytes);
    expect(verify([path.join(root, 'scripts/corpus/verify_office_edit.py'), 'docx', a, b, oldText, `${oldText} (reviewed)`])).toBe('OK');
    const folder = await folderOf(page, 'Health Quote Format.docx');
    expect(folder.versions.map((v) => v.parent_number)).toEqual([null, 1]); expect(sha(await bytesOf(page, folder, 1))).toBe(sha(original));
    await page.getByRole('button', { name: /Compare with v1/ }).click();
    await expect(page.locator('.change-after .diff-add')).toContainText('reviewed');
    await expect(page.getByTestId('viewer-target').locator('canvas').first()).toBeVisible({ timeout: 60_000 });
    await page.screenshot({ path: path.join(evidence, 'docx-compare.png'), fullPage: true });
  });

  test('4 Signed Motor policy: truthful status, acknowledged unsigned working copy, edit, download and reopen independently, lineage, immutable signed original', async ({ page }) => {
    const original = fs.readFileSync(files.motor);
    await upload(page, files.motor);
    const status = page.getByTestId('signature-status'); await expect(status).toBeVisible({ timeout: 30_000 });
    await expect(status).toHaveAttribute('data-state', 'integrity_valid_trust_not_established');
    await expect(status).toContainText('Not validated as trusted');
    await page.locator('.tab', { hasText: 'Edits' }).click();
    await expect(page.locator('.pdf-run')).toHaveCount(0);
    await page.getByTestId('create-working-copy').click();
    await page.getByRole('dialog').getByRole('checkbox').check(); await page.getByTestId('confirm-working-copy').click();
    await expect(page.locator('.file-meta')).toContainText('Version 2 of 2', { timeout: 120_000 });
    await expect(page.getByTestId('working-copy-banner').first()).toContainText('DRAFT — MODIFIED COPY — NOT OFFICIAL');
    const runs = page.locator('.pdf-run:not([data-locked])'); await expect(runs.first()).toBeVisible({ timeout: 60_000 });
    const labels = await runs.evaluateAll((els) => els.map((e) => ({ id: e.dataset.run, label: e.getAttribute('aria-label') })));
    const pick = labels.find((l) => /^Edit text: .{14,36}$/.test(l.label) && l.label.replace('Edit text: ', '').includes(' ') && !/OFFICIAL|working copy/i.test(l.label));
    const oldText = pick.label.replace('Edit text: ', ''); const next = oldText.split(' ').slice(0, -1).join(' ');
    await page.locator(`[data-run="${pick.id}"]`).click(); const input = page.locator('.pdf-run-input'); await input.fill(next); await input.press('Enter');
    await expect(page.locator(`.pdf-run.changed[data-run="${pick.id}"]`)).toBeVisible({ timeout: 60_000 });
    await saveNext(page, 3);
    await expect(page.getByTestId('working-copy-banner').first()).toBeVisible();
    const v3 = await exportFile(page);
    const folder = await folderOf(page, 'Motor  Policy.pdf');
    const v2file = path.join(scratch, 'motor-v2.pdf'), v3file = path.join(scratch, 'motor-v3.pdf'); fs.writeFileSync(v2file, await bytesOf(page, folder, 2)); fs.writeFileSync(v3file, v3.bytes);
    expect(verify([path.join(root, 'scripts/corpus/verify_pdf_text.py'), v2file, v3file, oldText, next])).toBe('OK');
    expect(v3.bytes.includes('/ByteRange')).toBe(false); expect(v3.bytes.includes('/Type /Sig') || v3.bytes.includes('/Type/Sig')).toBe(false);
    expect(execFileSync(python, ['-c', 'import sys,pypdf;r=pypdf.PdfReader(sys.argv[1]);print(all("DRAFT" in (p.extract_text() or "") and "NOTOFFICIAL" in (p.extract_text() or "").replace(" ","") for p in r.pages))', v3file]).toString().trim()).toBe('True');
    expect(folder.versions.map((v) => v.parent_number)).toEqual([null, 1, 2]);
    expect(folder.versions.map((v) => v.classification)).toEqual(['original', 'unsigned_working_copy', 'unsigned_working_copy']);
    expect(sha(await bytesOf(page, folder, 1))).toBe(sha(original));                      // the signed original is byte-identical
    await page.getByRole('button', { name: /All versions/ }).click();
    await expect(page.locator('.version-item').nth(1)).toContainText('Unsigned working copy · not official');
    await expect(page.locator('.version-item').nth(0).getByTestId('version-signature')).toContainText('signer not trusted');
    await page.locator('.version-item').filter({ hasText: 'v3' }).getByLabel('Compare v3 with another version').selectOption({ label: 'v1 · Original' });
    await expect(page.getByTestId('relation')).toContainText('v1 → v2 → v3');
    await expect(page.locator('.audit-lines')).toContainText('Unsigned working copy · not official');
    await page.screenshot({ path: path.join(evidence, 'motor-compare.png'), fullPage: true });
  });
});
