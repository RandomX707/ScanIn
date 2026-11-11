import { test, expect } from '@playwright/test';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const python = path.join(root, '.venv/bin/python');
const fixtureDir = path.join(root, '.runtime/browser-fixtures');
const mime = {
  txt: 'text/plain', md: 'text/markdown', csv: 'text/csv', json: 'application/json',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', pdf: 'application/pdf',
};
const unique = (stem, ext) => `${stem}-${crypto.randomBytes(4).toString('hex')}.${ext}`;
const sha = (buffer) => crypto.createHash('sha256').update(buffer).digest('hex');
const modifierKey = process.platform === 'darwin' ? 'Meta' : 'Control';

function verify(kind, file, expected, field) {
  return execFileSync(python, [path.join(root, 'scripts/host/verify_saved_download.py'), kind, file, expected, ...(field ? [field] : [])]).toString().trim();
}

test.beforeAll(() => {
  execFileSync(python, [path.join(root, 'scripts/host/create_browser_fixtures.py'), '--output', fixtureDir]);
});

async function upload(page, name, ext, buffer) {
  await page.goto('/');
  await page.setInputFiles('#file-input', { name, mimeType: mime[ext] ?? 'application/octet-stream', buffer });
  await expect(page.locator('.file-main h2')).toHaveText(name, { timeout: 60_000 });
}

async function download(page, trigger) {
  const [file] = await Promise.all([page.waitForEvent('download'), trigger()]);
  return { file, bytes: fs.readFileSync(await file.path()), name: file.suggestedFilename() };
}

async function exactSource(page, original) {
  const { bytes } = await download(page, () => page.locator('.reader-toolbar').getByRole('button', { name: 'Download exact source' }).click());
  expect(sha(bytes)).toBe(sha(original));
}

async function chunkNavigation(page) {
  await page.locator('.tab', { hasText: 'Chunks' }).click();
  const counter = page.locator('.chunk-pagination .stage-note');
  const text = await counter.textContent();
  const total = Number(text.split(' of ')[1]);
  expect(total).toBeGreaterThanOrEqual(1);
  if (total > 1) {
    await page.getByRole('button', { name: /Next chunk/ }).click();
    await expect(counter).toHaveText(`2 of ${total}`);
    await page.getByRole('button', { name: /Previous chunk/ }).click();
    await expect(counter).toHaveText(`1 of ${total}`);
  } else {
    await expect(page.getByRole('button', { name: /Next chunk/ })).toBeDisabled();
  }
}

async function saveNextVersion(page) {
  await expect(page.locator('#edit-state')).toHaveText('Unsaved changes');
  await page.getByRole('button', { name: 'Save as next version' }).click();
  await expect(page.locator('.toast')).toContainText('saved inside', { timeout: 120_000 });
  await expect(page.locator('.file-meta')).toContainText('Version 2 of 2');
}

async function checkFolderAndVersions(page, { name, ext, original, kind, expected, field }) {
  // folder shows v1 and v2
  await page.getByRole('button', { name: /All versions/ }).click();
  await expect(page.locator('.version-item')).toHaveCount(2);
  await expect(page.locator('.version-card').first()).toContainText('Original source');
  await expect(page.locator('.version-card').nth(1)).toContainText('Saved');
  // v2: same extension, opens with the matching library, contains the change
  await page.getByRole('button', { name: /Open v2/ }).click();
  await page.locator('.tab', { hasText: 'Edits' }).click();
  const v2 = await download(page, () => page.getByRole('button', { name: /^Export/ }).click());
  expect(path.extname(v2.name)).toBe(`.${ext}`);
  const out = path.join(fixtureDir, `v2-${v2.name}`);
  fs.writeFileSync(out, v2.bytes);
  expect(verify(kind, out, expected, field)).toBe('OK');
  // v1 stays byte-identical to the upload
  await page.locator('.version-pill', { hasText: 'v1' }).click();
  await exactSource(page, original);
  const v1 = await download(page, () => page.getByRole('button', { name: 'Exact source', exact: true }).click());
  expect(sha(v1.bytes)).toBe(sha(original));
  expect(name).toBeTruthy();
}

test.describe('Scan In workspace: upload, read, edit, save as next version', () => {
  test('TXT', async ({ page }) => {
    const name = unique('notes', 'txt');
    const original = Buffer.from('Service notes\nStandard requests wait two working days.\n');
    await upload(page, name, 'txt', original);
    await exactSource(page, original);
    await chunkNavigation(page);
    await page.locator('.tab', { hasText: 'Edits' }).click();
    await expect(page.locator('.editor-toolbar')).toHaveCount(0);
    await page.getByLabel('Editable working copy').fill('Service notes\nStandard requests wait three working days.\n');
    await saveNextVersion(page);
    await checkFolderAndVersions(page, { name, ext: 'txt', original, kind: 'txt', expected: 'three working days' });
  });

  test('MD (formatting toolbar)', async ({ page }) => {
    const name = unique('change-log', 'md');
    const original = Buffer.from('# Change log\n\n## Version 1\nInitial release.\n\n## Next steps\nPlan the rollout.\n');
    await upload(page, name, 'md', original);
    await exactSource(page, original);
    await chunkNavigation(page);
    await page.locator('.tab', { hasText: 'Edits' }).click();
    await expect(page.getByRole('toolbar', { name: 'Editor formatting' })).toBeVisible();
    const area = page.getByLabel('Editable working copy');
    await area.fill('# Change log\n\n## Version 1\nInitial release shipped.\n\n## Next steps\nPlan the rollout.\n');
    await saveNextVersion(page);
    await checkFolderAndVersions(page, { name, ext: 'md', original, kind: 'md', expected: 'Initial release shipped.' });
  });

  test('CSV', async ({ page }) => {
    const name = unique('claims', 'csv');
    const original = Buffer.from('Month,Submitted\nJanuary,340\nFebruary,387\n');
    await upload(page, name, 'csv', original);
    await exactSource(page, original);
    await chunkNavigation(page);
    await page.locator('.tab', { hasText: 'Edits' }).click();
    await page.locator('[data-cell="csv:2:2"]').fill('999');
    await saveNextVersion(page);
    await checkFolderAndVersions(page, { name, ext: 'csv', original, kind: 'csv', expected: '999' });
  });

  test('JSON', async ({ page }) => {
    const name = unique('members', 'json');
    const original = Buffer.from('{\n  "document": "Member roster",\n  "plan": "Corporate Gold",\n  "active": 26\n}\n');
    await upload(page, name, 'json', original);
    await exactSource(page, original);
    await chunkNavigation(page);
    await page.locator('.tab', { hasText: 'Edits' }).click();
    await expect(page.locator('.editor-toolbar')).toHaveCount(0);
    await page.getByLabel('Editable working copy').fill('{\n  "document": "Member roster",\n  "plan": "Corporate Platinum",\n  "active": 26\n}\n');
    await saveNextVersion(page);
    await checkFolderAndVersions(page, { name, ext: 'json', original, kind: 'json', expected: 'Corporate Platinum' });
  });

  test('XLSX', async ({ page }) => {
    const name = unique('enrollment', 'xlsx');
    const original = fs.readFileSync(path.join(root, 'packages/api/samples/Member_Enrollment_Register.xlsx'));
    await upload(page, name, 'xlsx', original);
    await exactSource(page, original);
    await chunkNavigation(page);
    await page.locator('.tab', { hasText: 'Edits' }).click();
    await page.locator('[data-cell="xlsx:Enrollments!B2"]').fill('Alexandra Morgan');
    await saveNextVersion(page);
    await checkFolderAndVersions(page, { name, ext: 'xlsx', original, kind: 'xlsx', expected: 'Alexandra Morgan' });
  });

  test('DOCX', async ({ page }) => {
    const name = unique('onboarding', 'docx');
    const original = fs.readFileSync(path.join(root, 'packages/api/samples/Employee_Onboarding_Guide.docx'));
    await upload(page, name, 'docx', original);
    await exactSource(page, original);
    await chunkNavigation(page);
    await page.locator('.tab', { hasText: 'Edits' }).click();
    await expect(page.getByRole('button', { name: /^B$/ })).toHaveCount(0); // no bold/italic/heading buttons
    const target = page.locator('[data-target]').first();
    await target.click();
    await page.keyboard.press('End');
    await page.keyboard.type(' (revised)');
    await saveNextVersion(page);
    await checkFolderAndVersions(page, { name, ext: 'docx', original, kind: 'docx', expected: '(revised)' });
  });

  test('Fillable PDF', async ({ page }) => {
    const name = unique('claim-form', 'pdf');
    const original = fs.readFileSync(path.join(fixtureDir, 'synthetic-form.pdf'));
    await upload(page, name, 'pdf', original);
    await exactSource(page, original);
    await chunkNavigation(page);
    await page.locator('.tab', { hasText: 'Edits' }).click();
    await page.locator('[data-field="claimant_name"]').fill('Adele Example');
    await saveNextVersion(page);
    await checkFolderAndVersions(page, { name, ext: 'pdf', original, kind: 'pdf', expected: 'Adele Example', field: 'claimant_name' });
  });

  test('scanned PDF is read only with no readable text', async ({ page }) => {
    const name = unique('scan', 'pdf');
    await upload(page, name, 'pdf', fs.readFileSync(path.join(fixtureDir, 'synthetic-scanned.pdf')));
    await page.locator('.tab', { hasText: 'Chunks' }).click();
    await expect(page.getByRole('heading', { name: 'No readable text detected' })).toBeVisible();
    await page.locator('.tab', { hasText: 'Edits' }).click();
    await expect(page.locator('.empty')).toContainText("can't be edited");
    await expect(page.locator('textarea, [contenteditable], .cell-input, .pdf-field')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Save as next version' })).toHaveCount(0);
  });

  test('a value too long for a PDF field shows a plain error and saves nothing', async ({ page }) => {
    const name = unique('claim-form-long', 'pdf');
    await upload(page, name, 'pdf', fs.readFileSync(path.join(fixtureDir, 'synthetic-form.pdf')));
    await page.locator('.tab', { hasText: 'Edits' }).click();
    const field = page.locator('[data-field="claimant_name"]');
    await field.fill('X'.repeat(80));
    await page.getByRole('button', { name: 'Save as next version' }).click();
    await expect(page.locator('.toast.error')).toHaveText("This value doesn't fit the field.");
    await expect(field).toHaveValue('X'.repeat(80)); // the user's edit is kept
    await expect(page.locator('.file-meta')).toContainText('Version 1 of 1');
    await page.getByRole('button', { name: /All versions/ }).click();
    await expect(page.locator('.version-item')).toHaveCount(1);
  });

  test('an XLSX value with formula dependents saves with the recalculation note', async ({ page }) => {
    const name = unique('dependent', 'xlsx');
    const original = fs.readFileSync(path.join(root, 'artifacts/m0/m0_dependent_original.xlsx'));
    await upload(page, name, 'xlsx', original);
    await page.locator('.tab', { hasText: 'Edits' }).click();
    await expect(page.locator('.cell-locked[title^="Formula cell"]').first()).toBeVisible();
    await page.locator('[data-cell="xlsx:Claim!D2"]').fill('130.25');
    await page.getByRole('button', { name: 'Save as next version' }).click();
    await expect(page.locator('.toast')).toContainText('Formulas update when opened in Excel.', { timeout: 120_000 });
    await expect(page.locator('.file-meta')).toContainText('Version 2 of 2');
    const v2 = await download(page, () => page.getByRole('button', { name: /^Export/ }).click());
    expect(path.extname(v2.name)).toBe('.xlsx');
    const out = path.join(fixtureDir, `v2-${v2.name}`);
    fs.writeFileSync(out, v2.bytes);
    expect(verify('xlsx', out, '130.25')).toBe('OK');
  });

  for (const [label, file, ext, sentence] of [
    ['signed PDF', 'synthetic-signed.pdf', 'pdf', /signed|signature/i],
    ['XFA PDF', 'synthetic-xfa.pdf', 'pdf', /XFA/],
    ['PDF without form fields', 'synthetic-invoice-no-fields.pdf', 'pdf', /no form fields/],
    ['tracked-changes DOCX', 'synthetic-tracked-changes.docx', 'docx', /tracked changes/],
  ]) {
    test(`${label} is read only with one plain sentence`, async ({ page }) => {
      await upload(page, unique('readonly', ext), ext, fs.readFileSync(path.join(fixtureDir, file)));
      await page.locator('.tab', { hasText: 'Edits' }).click();
      await expect(page.locator('.empty p')).toHaveText(sentence);
      await expect(page.locator('textarea, [contenteditable], .cell-input, .pdf-field')).toHaveCount(0);
    });
  }

  test('an encrypted PDF is refused rather than opened for editing', async ({ page }) => {
    await page.goto('/');
    await page.setInputFiles('#file-input', { name: unique('locked', 'pdf'), mimeType: 'application/pdf', buffer: fs.readFileSync(path.join(fixtureDir, 'synthetic-encrypted.pdf')) });
    await expect(page.locator('.toast.error')).toContainText('could not be read safely');
    await expect(page.locator('.file-main')).toHaveCount(0);
  });

  test('PPTX and images are read only', async ({ page }) => {
    for (const [file, ext] of [['Quarterly_Service_Review.pptx', 'pptx'], ['Claim_Receipt.png', 'png']]) {
      await upload(page, unique('readonly', ext), ext, fs.readFileSync(path.join(root, 'packages/api/samples', file)));
      await page.locator('.tab', { hasText: 'Edits' }).click();
      await expect(page.locator('.empty p')).toContainText('can be');
      await expect(page.locator('textarea, [contenteditable], .cell-input, .pdf-field')).toHaveCount(0);
    }
  });

  test('History search finds a folder', async ({ page }) => {
    const stem = `findable-${crypto.randomBytes(4).toString('hex')}`;
    await upload(page, `${stem}.txt`, 'txt', Buffer.from('Searchable folder content.\n'));
    await page.getByRole('button', { name: 'History' }).first().click();
    await page.getByLabel('Search document folders').fill(stem);
    await expect(page.locator('.history-item')).toHaveCount(1);
    await expect(page.locator('.history-item')).toContainText(stem);
    await page.getByLabel('Search document folders').fill(`${stem}-missing`);
    await expect(page.getByRole('heading', { name: 'No folders found' })).toBeVisible();
  });

  test('Try demo opens the Meridian sample with v2 and v3', async ({ page }) => {
    await page.goto('/');
    await page.getByRole('button', { name: /Try demo/ }).click();
    await expect(page.locator('.file-main h2')).toContainText('Meridian');
    await expect(page.locator('.sample-ribbon')).toContainText('SYNTHETIC SAMPLE');
    await expect(page.locator('.version-pill')).toHaveCount(3);
  });
});
