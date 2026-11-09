// Phase 3A item 2: viewable, natively editable and sidecar-correctable are separate capabilities.
import { test, expect } from '@playwright/test';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const api = process.env.IDW_API_BASE_URL || 'http://127.0.0.1:8000';
const owner = process.env.IDW_DEMO_OWNER_TOKEN;
const fixtures = path.resolve(here, '../../../.runtime/browser-fixtures');
const XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';

async function apiCall(request, route, options = {}) {
  return request.fetch(`${api}${route}`, { ...options, headers: { Authorization: `Bearer ${owner}`, ...(options.headers || {}) } });
}

async function upload(request, filename, bytes, mime, inspect = true) {
  const extension = path.extname(filename);
  const unique = `${filename.slice(0, -extension.length)}-${crypto.randomUUID()}${extension}`;
  const response = await apiCall(request, `/api/documents?filename=${encodeURIComponent(unique)}`, { method: 'PUT', data: bytes, headers: { 'Content-Type': mime } });
  expect(response.ok()).toBeTruthy();
  const body = await response.json();
  const inspected = inspect ? await (await apiCall(request, `/api/documents/${body.document_id}/inspect?include_targets=true`, { method: 'POST' })).json() : null;
  return { ...body, filename: unique, inspected };
}

async function open(page, filename) {
  await page.goto('/');
  if (await page.evaluate((token) => sessionStorage.getItem('channel360.demo.session') !== token, owner)) {
    await page.evaluate((token) => { sessionStorage.setItem('channel360.demo.session', token); sessionStorage.setItem('channel360.demo.role', 'OWNER'); }, owner);
    await page.reload();
  }
  await page.getByRole('button', { name: 'Documents' }).click();
  await page.getByRole('textbox', { name: 'Search documents' }).fill(filename);
  await page.getByRole('button', { name: `Open ${filename}` }).click();
  await expect(page.locator('.source-info, .preview-unavailable').first()).toBeVisible({ timeout: 60_000 });
}

const cell = (page, id) => page.locator(`.grid-value[title^="${id} "]`);

test('rich-text cell shows its text, is read only for native patches and stays correctable', async ({ page, request }) => {
  const uploaded = await upload(request, 'synthetic-rich-text.xlsx', fs.readFileSync(path.join(fixtures, 'synthetic-rich-text.xlsx')), XLSX);
  const rich = uploaded.inspected.targets.find((target) => target.id === 'xlsx:Claim!B2');
  expect(rich).toMatchObject({ editable: false, reason: 'rich_text', value: { type: 'string', value: 'Meera Iyer' } });
  await open(page, uploaded.filename);
  await expect(cell(page, 'xlsx:Claim!B2')).toHaveText('Meera Iyer');
  await expect(cell(page, 'xlsx:Claim!B2')).toHaveClass(/read-only-target/);
  await expect(cell(page, 'xlsx:Claim!B3')).not.toHaveClass(/read-only-target/);
  await expect(page.locator('.source-grid')).not.toContainText('null');
  await page.getByRole('tab', { name: 'Native changes' }).click();
  await page.getByLabel('Document category').selectOption('insurer-authored');
  await page.getByRole('button', { name: 'Save classification' }).click();
  await page.getByText('Insurer-Authored category').waitFor();
  await page.getByLabel('Reason for document change').fill('Phase 3A read-only gating');
  await page.getByRole('button', { name: 'Create correction draft' }).click();
  await cell(page, 'xlsx:Claim!B2').click();
  await expect(page.locator('.selected-source')).toContainText('Read only · data corrections only');
  await expect(page.getByRole('button', { name: 'Add selected target patch' })).toBeDisabled();
  await expect(page.locator('.workflow-status')).toContainText('can be recorded as a data correction but not changed in the file');
  await cell(page, 'xlsx:Claim!B3').click();
  await expect(page.getByRole('button', { name: 'Add selected target patch' })).toBeEnabled();
  await cell(page, 'xlsx:Claim!B2').click();
  await page.getByRole('button', { name: 'Add to data corrections →' }).click();
  await expect(page.locator('.toast')).toContainText('Field added to the correction workspace');
  const fields = await (await apiCall(request, `/api/documents/${uploaded.document_id}/fields`)).json();
  expect(fields.items[0]).toMatchObject({ target_id: 'xlsx:Claim!B2', observed_value: { value: 'Meera Iyer' } });
});

test('macro workbook: every target read only, capabilities reported separately', async ({ page, request }) => {
  const uploaded = await upload(request, 'synthetic-macro.xlsm', fs.readFileSync(path.join(fixtures, 'synthetic-macro.xlsm')), 'application/vnd.ms-excel.sheet.macroEnabled.12');
  expect(uploaded.inspected.capability).toEqual({ level: 'Read-only', reasons: ['macros_present'] });
  expect(uploaded.inspected.targets.every((target) => target.editable === false)).toBeTruthy();
  expect(uploaded.inspected.capabilities).toEqual({
    viewable: { available: true, reason: null },
    native_editing: { available: false, reason: 'macros_present' },
    data_corrections: { available: true, reason: null },
  });
  await open(page, uploaded.filename);
  expect(await page.locator('.grid-value[data-editable="true"]').count()).toBe(0);
  expect(await page.locator('.grid-value.read-only-target').count()).toBeGreaterThan(0);
  await page.getByRole('tab', { name: 'Inspect' }).click();
  await expect(page.locator('[data-capability="viewable"]')).toHaveText('Available');
  await expect(page.locator('[data-capability="native_editing"]')).toHaveText('Unavailable · Macros Present');
  await expect(page.locator('[data-capability="data_corrections"]')).toHaveText('Available');
});

test('legacy .doc is not reported viewable and shows why', async ({ page, request }) => {
  const legacy = Buffer.concat([Buffer.from('d0cf11e0a1b11ae1', 'hex'), Buffer.from('synthetic legacy word placeholder')]);
  const uploaded = await upload(request, 'synthetic-legacy.doc', legacy, 'application/msword');
  expect(uploaded.inspected.viewing_ready).toBe(false);
  expect(uploaded.inspected.capabilities.viewable).toEqual({ available: false, reason: 'legacy_office_unsupported' });
  const view = await apiCall(request, `/api/documents/${uploaded.document_id}/versions/${uploaded.version_id}/view`);
  expect(view.status()).toBe(415);
  await open(page, uploaded.filename);
  await expect(page.locator('.unavailable-reason')).toContainText('legacy_office_unsupported');
  await expect(page.locator('.unavailable-reason')).toContainText('legacy binary Office file');
});

test('rejected encrypted PDF shows its actual rejection reason', async ({ page, request }) => {
  const uploaded = await upload(request, 'synthetic-encrypted.pdf', fs.readFileSync(path.join(fixtures, 'synthetic-encrypted.pdf')), 'application/pdf', false);
  await open(page, uploaded.filename);
  await expect(page.getByRole('heading', { name: 'Safe preview is unavailable' })).toBeVisible();
  await expect(page.locator('.unavailable-reason')).toContainText('Rejected during safe inspection');
  await expect(page.locator('.unavailable-reason')).toContainText('encrypted_pdf');
  await expect(page.locator('.toast')).toContainText('Rejected: The PDF is password protected');
  const original = await apiCall(request, `/api/documents/${uploaded.document_id}/original`);
  expect(original.ok()).toBeTruthy();
});
