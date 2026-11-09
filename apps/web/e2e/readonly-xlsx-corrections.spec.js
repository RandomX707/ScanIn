// B-1: read-only XLSX values (protected, unsupported validation, rich text, time, formula) stay viewable once,
// accept sidecar data corrections and are refused for native patching. Corrections never create a version.
import { test, expect } from '@playwright/test';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const api = process.env.IDW_API_BASE_URL || 'http://127.0.0.1:8000';
const owner = process.env.IDW_DEMO_OWNER_TOKEN;
const otherTenant = process.env.IDW_DEMO_OTHER_TENANT_TOKEN;
const fixture = path.resolve(here, '../../../.runtime/browser-fixtures/synthetic-readonly-values.xlsx');
const sha = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');
const READ_ONLY = {
  'xlsx:Locked!A1': ['protected_cell_locked', 'Locked reserve amount'],
  'xlsx:Claim!B6': ['unsupported_data_validation', 'Pending'],
  'xlsx:Claim!B4': ['rich_text', 'Urgent: tow required'],
  'xlsx:Claim!B5': ['time_value_read_only', '15:30:00'],
  'xlsx:Claim!B3': ['formula_cell', '=LEN(B2)'],
};

async function call(request, method, route, data, token = owner) {
  const response = await request.fetch(`${api}${route}`, { method, data, headers: { Authorization: `Bearer ${token}` } });
  let body = null; try { body = await response.json(); } catch { /* binary */ }
  return { status: response.status(), body, response };
}

async function upload(request) {
  const bytes = fs.readFileSync(fixture);
  const filename = `readonly-values-${crypto.randomUUID()}.xlsx`;
  const uploaded = await request.fetch(`${api}/api/documents?filename=${filename}`, { method: 'PUT', data: bytes,
    headers: { Authorization: `Bearer ${owner}`, 'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' } });
  const doc = await uploaded.json();
  expect((await call(request, 'POST', `/api/documents/${doc.document_id}/inspect?include_targets=true`)).status).toBe(200);
  return { ...doc, filename, bytes };
}

test('read-only XLSX values are viewable once, correctable as sidecars and never natively patchable', async ({ request }) => {
  const doc = await upload(request);
  const view = (await call(request, 'GET', `/api/documents/${doc.document_id}/versions/${doc.version_id}/view`)).body;
  const ids = view.targets.map((t) => t.id);
  expect(new Set(ids).size).toBe(ids.length);
  expect(view.targets_page.total).toBe(ids.length);
  const byId = Object.fromEntries(view.targets.map((t) => [t.id, t]));
  for (const [id, [reason, value]] of Object.entries(READ_ONLY)) {
    expect(byId[id], id).toMatchObject({ editable: false, reason, value: { value } });
  }
  expect(byId['xlsx:Claim!B2']).toMatchObject({ editable: true, reason: null });
  expect(byId['xlsx:Locked!A2']).toMatchObject({ editable: true, reason: null });
  const lookup = (await call(request, 'GET', `/api/documents/${doc.document_id}/versions/${doc.version_id}/view?target_id=${encodeURIComponent('xlsx:Locked!A1')}`)).body;
  expect(lookup.targets).toEqual([byId['xlsx:Locked!A1']]);

  expect((await call(request, 'PUT', `/api/documents/${doc.document_id}/category`, { category: 'insurer-authored' })).status).toBe(200);
  const draft = (await call(request, 'POST', `/api/documents/${doc.document_id}/edit-sets`, { reason: 'B-1 native refusal' })).body;
  for (const id of Object.keys(READ_ONLY)) {
    const target = byId[id];
    const nativePatch = await call(request, 'PUT', `/api/documents/${doc.document_id}/edit-sets/${draft.edit_set_id}/patches`,
      { patches: [{ target_id: id, fingerprint: target.fingerprint, new_value: { type: 'string', value: 'changed' } }] });
    expect([nativePatch.status, nativePatch.body.code], `native patch ${id}`).toEqual([409, 'target_not_editable_or_stale']);
    const field = await call(request, 'POST', `/api/documents/${doc.document_id}/fields`, { target_id: id, fingerprint: target.fingerprint });
    expect(field.status, `field ${id}`).toBe(200);
    expect(field.body.evidence[0]).toMatchObject({ kind: 'native_target', natively_editable: false, native_reason: READ_ONLY[id][0],
      source_version_id: doc.version_id, source_sha256: sha(doc.bytes) });
    const correction = await call(request, 'POST', `/api/documents/${doc.document_id}/fields/${field.body.field_id}/corrections`,
      { corrected_value: { type: 'string', value: `${READ_ONLY[id][1]} (corrected)` }, reason: `B-1 sidecar for ${id}` });
    expect(correction.status, `correction ${id}`).toBe(200);
  }
  const foreign = await call(request, 'POST', `/api/documents/${doc.document_id}/fields`,
    { target_id: 'xlsx:Locked!A1', fingerprint: byId['xlsx:Locked!A1'].fingerprint }, otherTenant);
  expect(foreign.status).toBe(404);

  const exported = (await call(request, 'GET', `/api/documents/${doc.document_id}/corrections/export`)).body;
  expect(exported).toHaveLength(Object.keys(READ_ONLY).length);
  for (const item of exported) {
    expect(item.source_version_id).toBe(doc.version_id);
    expect(item.source_sha256).toBe(sha(doc.bytes));
    expect(item.target_fingerprint).toBe(byId[item.target_id].fingerprint);
  }
  const versions = (await call(request, 'GET', `/api/documents/${doc.document_id}/versions`)).body.items;
  expect(versions.map((v) => v.version_id)).toEqual([doc.version_id]);
  const original = await request.fetch(`${api}/api/documents/${doc.document_id}/original`, { headers: { Authorization: `Bearer ${owner}` } });
  expect(sha(Buffer.from(await original.body()))).toBe(sha(doc.bytes));
});

test('browser: a protected cell is selectable for a sidecar correction but never for a native patch', async ({ page, request }) => {
  const doc = await upload(request);
  await page.goto('/');
  if (await page.evaluate((token) => sessionStorage.getItem('channel360.demo.session') !== token, owner)) {
    await page.evaluate((token) => { sessionStorage.setItem('channel360.demo.session', token); sessionStorage.setItem('channel360.demo.role', 'OWNER'); }, owner);
    await page.reload();
  }
  await page.getByRole('button', { name: 'Documents' }).click();
  await page.getByRole('textbox', { name: 'Search documents' }).fill(doc.filename);
  await page.getByRole('button', { name: `Open ${doc.filename}` }).click();
  await expect(page.locator('.source-info')).toBeVisible({ timeout: 60_000 });
  await page.getByRole('tab', { name: 'Locked' }).click();
  const locked = page.locator('.grid-value[title^="xlsx:Locked!A1 "]');
  await expect(locked).toHaveText('Locked reserve amount');
  await expect(locked).toHaveClass(/read-only-target/);
  await expect(locked).toHaveAttribute('data-editable', 'false');
  await expect(page.locator('.grid-value[title^="xlsx:Locked!A2 "]')).toHaveAttribute('data-editable', 'true');

  await page.getByRole('tab', { name: 'Native changes' }).click();
  await page.getByLabel('Document category').selectOption('insurer-authored');
  await page.getByRole('button', { name: 'Save classification' }).click();
  await page.getByText('Insurer-Authored category').waitFor();
  await page.getByLabel('Reason for document change').fill('B-1 native gating');
  await page.getByRole('button', { name: 'Create correction draft' }).click();
  await locked.click();
  await expect(page.locator('.selected-source')).toContainText('Read only · data corrections only');
  await expect(page.getByRole('button', { name: 'Add selected target patch' })).toBeDisabled();
  await expect(page.locator('.workflow-status')).toContainText('Protected Cell Locked');

  await page.getByRole('button', { name: 'Add to data corrections →' }).click();
  await expect(page.locator('.toast')).toContainText('Field added to the correction workspace');
  await page.getByRole('tab', { name: 'Data corrections' }).click();
  const card = page.locator('.correction-card').filter({ hasText: 'xlsx:Locked!A1' });
  await expect(card).toContainText('Read-only source value');
  await card.getByLabel('Corrected value').fill('Locked reserve amount: 18,250.00');
  await card.getByLabel('Correction value type').selectOption('string');
  await card.getByLabel('Correction reason').fill('Reserve confirmed with the claims ledger');
  await card.getByRole('button', { name: 'Save data correction' }).click();
  await expect(page.locator('.toast')).toContainText('Data correction saved separately');
  await expect(page.locator('.correction-history-row').filter({ hasText: 'Locked reserve amount: 18,250.00' })).toBeVisible();

  // The browser draft received no native patch: preflight refuses an empty patch set.
  const edits = (await call(request, 'GET', `/api/documents/${doc.document_id}/edit-sets`)).body.items;
  expect(edits).toHaveLength(1);
  const preflight = await call(request, 'POST', `/api/documents/${doc.document_id}/edit-sets/${edits[0].edit_set_id}/preflight`);
  expect([preflight.status, preflight.body.code]).toEqual([422, 'patch_set_empty']);
  const versions = (await call(request, 'GET', `/api/documents/${doc.document_id}/versions`)).body.items;
  expect(versions).toHaveLength(1);
});
