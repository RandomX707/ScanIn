// Phase 3A item 3: small UI correctness defects (N9-N12) and document-wide editable counts.
import { test, expect } from '@playwright/test';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const api = process.env.IDW_API_BASE_URL || 'http://127.0.0.1:8000';
const owner = process.env.IDW_DEMO_OWNER_TOKEN;
const fixtures = path.resolve(here, '../../../.runtime/browser-fixtures');

async function upload(request, filename, bytes, mime) {
  const extension = path.extname(filename);
  const unique = `${filename.slice(0, -extension.length)}-${crypto.randomUUID()}${extension}`;
  const response = await request.fetch(`${api}/api/documents?filename=${encodeURIComponent(unique)}`, {
    method: 'PUT', data: bytes, headers: { Authorization: `Bearer ${owner}`, 'Content-Type': mime } });
  expect(response.ok()).toBeTruthy();
  const body = await response.json();
  expect((await request.fetch(`${api}/api/documents/${body.document_id}/inspect?include_targets=true`, {
    method: 'POST', headers: { Authorization: `Bearer ${owner}` } })).ok()).toBeTruthy();
  return { ...body, filename: unique };
}

async function signIn(page) {
  await page.goto('/');
  if (await page.evaluate((token) => sessionStorage.getItem('channel360.demo.session') !== token, owner)) {
    await page.evaluate((token) => { sessionStorage.setItem('channel360.demo.session', token); sessionStorage.setItem('channel360.demo.role', 'OWNER'); }, owner);
    await page.reload();
  }
}

async function open(page, filename) {
  await page.getByRole('button', { name: 'Documents' }).click();
  await page.getByRole('textbox', { name: 'Search documents' }).fill(filename);
  await page.getByRole('button', { name: `Open ${filename}` }).click();
  await expect(page.locator('.source-info, .preview-unavailable').first()).toBeVisible({ timeout: 60_000 });
}

test('N9: DOCX summary citing one block twice renders without duplicate React keys', async ({ page, request }) => {
  const warnings = [];
  page.on('console', (message) => { if (/same key|unique "key"/i.test(message.text())) warnings.push(message.text()); });
  const uploaded = await upload(request, 'synthetic-sections.docx', fs.readFileSync(path.join(fixtures, 'synthetic-sections.docx')),
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document');
  const view = await (await request.fetch(`${api}/api/documents/${uploaded.document_id}/versions/${uploaded.version_id}/view`, {
    headers: { Authorization: `Bearer ${owner}` } })).json();
  const references = view.structural_summary.evidence_references.map((item) => item.reference);
  expect(new Set(references).size).toBeLessThan(references.length);
  await signIn(page);
  await open(page, uploaded.filename);
  await page.getByRole('tab', { name: 'Summary' }).click();
  await expect(page.getByTestId('structural-facts')).toBeVisible();
  for (const tab of ['Inspect', 'Data corrections', 'Native changes', 'History', 'Summary']) await page.getByRole('tab', { name: tab }).click();
  expect(warnings).toEqual([]);
});

test('N10: error toasts use the error mark, success toasts the check mark', async ({ page, request }) => {
  const uploaded = await upload(request, 'browser-claims.csv', Buffer.from('claim,name\nC1,Asha\n'), 'text/csv');
  await signIn(page);
  await open(page, uploaded.filename);
  await expect(page.locator('.toast .toast-mark')).toHaveText('✓');
  await expect(page.locator('.toast .toast-mark')).not.toHaveClass(/error-mark/);
  await page.getByLabel('Go to cell').fill('999:9');
  await page.getByRole('button', { name: 'Find' }).click();
  await expect(page.locator('.toast')).toContainText('could not be found');
  await expect(page.locator('.toast .toast-mark')).toHaveClass(/error-mark/);
  await expect(page.locator('.toast .toast-mark')).toHaveText('!');
  await page.getByLabel('Go to cell').fill('2:2');
  await page.getByRole('button', { name: 'Find' }).click();
  await page.getByRole('tab', { name: 'Data corrections' }).click();
  await page.getByRole('button', { name: 'Add to data corrections →' }).click();
  await page.getByRole('tab', { name: 'Data corrections' }).click();
  await page.locator('.correction-card').first().getByRole('button', { name: 'Save data correction' }).click();
  await expect(page.locator('.toast')).toContainText('Add a reason before saving this correction.');
  await expect(page.locator('.toast .toast-mark')).toHaveClass(/error-mark/);
});

test('N11: the tenant name persists after a browser reload', async ({ page }) => {
  await signIn(page);
  await expect(page.locator('.workspace-switch strong')).toHaveText('Channel360 Browser E2E');
  await page.reload();
  await expect(page.getByRole('navigation', { name: 'Main navigation' })).toBeVisible();
  await expect(page.locator('.workspace-switch strong')).toHaveText('Channel360 Browser E2E');
});

test('N12 and counts: consistent capability labels and document-wide editable totals', async ({ page, request }) => {
  const rows = Array.from({ length: 1100 }, (_, index) => `C${index},N${index},${index},open`).join('\n');
  const large = await upload(request, 'browser-large.csv', Buffer.from(`claim,name,amount,status\n${rows}\n`), 'text/csv');
  const macro = await upload(request, 'synthetic-macro.xlsm', fs.readFileSync(path.join(fixtures, 'synthetic-macro.xlsm')),
    'application/vnd.ms-excel.sheet.macroEnabled.12');
  const signed = await upload(request, 'synthetic-signed.pdf', fs.readFileSync(path.join(fixtures, 'synthetic-signed.pdf')), 'application/pdf');
  await signIn(page);
  await open(page, large.filename);
  await page.getByRole('tab', { name: 'Inspect' }).click();
  await expect(page.getByTestId('capability-label')).toHaveText('Editable');
  await expect(page.getByTestId('editable-count')).toHaveText('4404 (of 4404 values; 2000 on this page)');
  for (const [document, label] of [[macro, 'Read only'], [signed, 'Read only']]) {
    await open(page, document.filename);
    await page.getByRole('tab', { name: 'Inspect' }).click();
    await expect(page.getByTestId('capability-label')).toHaveText(label);
    await expect(page.getByTestId('editable-count')).toHaveText('0');
  }
});
